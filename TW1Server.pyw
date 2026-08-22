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
    "ICAgcmV0dXJuIGJsYnVmCgojQ29tbWFuZCBmdW5jdGlvbnMKI0xldmVsIGlkcywgbW9kZSBpZHMg"
    "YW5kIHRyYW5zbGF0ZSBrZXlzIGFyZSBhbGwgYmFyZSBpZGVudGlmaWVycyBpbiBldmVyeSByb29t"
    "CiNzZWVuOiAiTmV0X01fMDEiLCAibnVsbCIsICJIYXJkIiwgInRyYW5zbGF0ZU5ldF9NXzAxIi4K"
    "X1JFX1JPT01fSUQgPSByZS5jb21waWxlKHInXltBLVphLXowLTlfXXsxLDY0fSQnKQpkZWYgX2lz"
    "Um9vbVBhcmFtcyh0ZXh0KToKICAgICNJcyB0aGlzIHRoZSByb29tIHBhcmFtZXRlciBzdHJpbmcg"
    "dGhlIGdhbWUgcGFyc2VzIGluIEluaXRpYWxpemVDYW1wYWlnbiAtCiAgICAjImxldmVsSUQgbW9k"
    "ZUlEfG51bGwgMHwxW2d1aWxkcyBnYW1lXSAwfDFbWEJveCByYW5rZWRdIiwgd2l0aCB0aGUgdHdv"
    "CiAgICAjZmxhZ3Mgb3B0aW9uYWwgYmVjYXVzZSB0aGUgcGFyc2VyIHN0b3BzIGF0IHdoaWNoZXZl"
    "ciBmaWVsZCBpcyBtaXNzaW5nPwogICAgI0RlbGliZXJhdGVseSBzdHJpY3Q6IHRoaXMgc3RyaW5n"
    "IGlzIGhhbmRlZCB0byBldmVyeSBjbGllbnQgaW4gdGhlIHRvd24KICAgICNpbnNpZGUgYSBxdW90"
    "ZWQgJGdhbWUgZmllbGQsIHNvIGFueXRoaW5nIHRoYXQgY291bGQgY2FycnkgYSBxdW90ZSwgYQog"
    "ICAgI25ld2xpbmUgb3IgYSBzZWNvbmQgY29tbWFuZCBpcyBub3QgYSByb29tIGRlc2NyaXB0aW9u"
    "LgogICAgcGFydHMgPSB0ZXh0LnNwbGl0KCkKICAgIGlmIG5vdCAyIDw9IGxlbihwYXJ0cykgPD0g"
    "NDoKICAgICAgICByZXR1cm4gRmFsc2UKICAgIGlmIG5vdCAoX1JFX1JPT01fSUQubWF0Y2gocGFy"
    "dHNbMF0pIGFuZCBfUkVfUk9PTV9JRC5tYXRjaChwYXJ0c1sxXSkpOgogICAgICAgIHJldHVybiBG"
    "YWxzZQogICAgcmV0dXJuIGFsbChwIGluICgnMCcsICcxJykgZm9yIHAgaW4gcGFydHNbMjpdKQpf"
    "UkVfSEVST19QT1MgPSByZS5jb21waWxlKHInXlswLTlBLUZhLWZdezEsOH0jWzAtOUEtRmEtZl17"
    "MSw4fSQnKQpkZWYgX2hlcm9Qb3MocmF3KToKICAgICMtPiAieHh4eCN5eXl5IiBvciBOb25lLgog"
    "ICAgIyBUaGUgY2xpZW50IHNlbmRzIGVpdGhlciAieHh4eCN5eXl5IiBvciAiVUlEI3h4eHgjeXl5"
    "eSIsIGJ1dCB1cGRhdGVQb3MoKQogICAgIyB1bmNvbmRpdGlvbmFsbHkgcHJlZml4ZXMgdGhlIHNl"
    "bmRlcidzIGlkIHdoZW4gaXQgZmFucyB0aGUgcG9zaXRpb24gb3V0LgogICAgIyBTdG9yaW5nIHRo"
    "ZSByYXcgZmllbGQgbWVhbnQgdGhlIHNlY29uZCBmb3JtIHdlbnQgYmFjayBvdXQgYXMKICAgICMg"
    "IlVJRCNVSUQjeHh4eCN5eXl5Iiwgd2hpY2ggbm8gY2xpZW50IGNhbiBtYXRjaCB0byBhIHBsYXll"
    "cjogdGhhdCBoZXJvJ3MKICAgICMgbWFya2VyIHRoZW4gc3RheWVkIHdoZXJldmVyIGl0IHdhcyBs"
    "YXN0IHN1Y2Nlc3NmdWxseSBwYXJzZWQgd2hpbGUgdGhlCiAgICAjIHBsYXllciBhY3R1YWxseSB3"
    "YWxrZWQgYXdheS4gS2VlcCBvbmx5IHRoZSB0cmFpbGluZyBjb29yZGluYXRlIHBhaXIgc28KICAg"
    "ICMgZXhhY3RseSBvbmUgaWQgaXMgcHJlc2VudCBvbiB0aGUgd2lyZSByZWdhcmRsZXNzIG9mIHdo"
    "YXQgd2FzIHNlbnQuCiAgICAjIEFueXRoaW5nIHRoYXQgaXMgbm90IGEgcGFpciBvZiBoZXggbnVt"
    "YmVycyBpcyBkaXNjYXJkZWQgcmF0aGVyIHRoYW4KICAgICMgc3RvcmVkOiB0aGlzIHZhbHVlIGlz"
    "IGNvcGllZCB2ZXJiYXRpbSBpbnRvIGEgYnJvYWRjYXN0IGV2ZXJ5IG90aGVyIGNsaWVudAogICAg"
    "IyBpbiB0aGUgdG93biBoYXMgdG8gcGFyc2UsIHNvIGEgc2luZ2xlIGp1bmsgZmllbGQgZnJvbSBv"
    "bmUgY2xpZW50CiAgICAjIChhIHRydW5jYXRlZCBwYWNrZXQsIGEgbW9kaWZpZWQgY2xpZW50KSBi"
    "ZWNhbWUgZXZlcnlvbmUgZWxzZSdzIHByb2JsZW0uCiAgICBwb3MgPSAnIycuam9pbihzdHIocmF3"
    "KS5zcGxpdCgnIycpWy0yOl0pCiAgICByZXR1cm4gcG9zIGlmIF9SRV9IRVJPX1BPUy5tYXRjaChw"
    "b3MpIGVsc2UgTm9uZQpkZWYgX25vcChtZCx1c3IscmVzKToKICAgIHJldHVybiBOb25lCmRlZiBf"
    "dXBkaGVyb3BvcyhtZCx1c3IscmVzKToKICAgIGlmIG5vdCB1c3IudXNlci5nYW1lY2hhbm5lbDoK"
    "ICAgICAgICByZXR1cm4gTm9uZSAjbm90IGluIGEgZ2FtZSBjaGFubmVsLCBpZ25vcmUKICAgIHBv"
    "cyA9IF9oZXJvUG9zKHJlc1sxXSkKICAgIGlmIHBvcyBpcyBOb25lOgogICAgICAgIHJldHVybiBO"
    "b25lICN1bnBhcnNlYWJsZSBjb29yZGluYXRlcywgc2VlIF9oZXJvUG9zCiAgICB1c3IudXNlci5w"
    "b3NkYXRhID0gcG9zCiAgICB1c3IudXNlci5nYW1lY2hhbm5lbC5kaXJ0eSA9IFRydWUKICAgIHVz"
    "ci51c2VyLnBvc2NoYW5nZWQgPSBUcnVlCiAgICByZXR1cm4gTm9uZSAjbm8gcmVzcG9uc2UKZGVm"
    "IF9zZXRwbGF5ZXJkYXRhKG1kLHVzcixyZXMpOgogICAgcGQgPSBfUmVhZEJsb2IodXNyLCByZXNb"
    "M10pCiAgICAjVE9ETyBDSEVDSyBwZXJtaXNzaW9ucyBmb3Igc2V0RGF0YShzZWxmIG9yIG90aGVy"
    "KQogICAgaWYgcmVzWzFdID09IHVzci51c2VyLm5hbWU6CiAgICAgICAgR0RILnNldFBsYXllckRh"
    "dGEocmVzWzFdLCByZXNbMl0sIHBkKQogICAgI1RPRE8gaGFuZGxlIHJlbWFpbmluZyB2YWx1ZXMK"
    "ICAgICNyZXNbeF06CiAgICAjMDogL3NldHBsYXllcmRhdGEKICAgICMxOiBuYW1lCiAgICAjMjog"
    "Zm9ybQogICAgIzM6IGJsb2JzaXplCiAgICAjNDogdW5rbm93biAocG9pbnRzPykKICAgICM1OiB1"
    "bmtub3duLCAxIChib29sPykKICAgIHJldHVybiBOb25lCmRlZiBfZ2V0cGxheWVyZGF0YShtZCx1"
    "c3IscmVzKToKICAgICNUT0RPIGNoZWNrIHBlcm1pc3Npb24gZm9yIGdldERhdGEoc2VsZiBvciBv"
    "dGhlcikKICAgIGlmIHJlc1sxXSA9PSB1c3IudXNlci5uYW1lOgogICAgICAgIHBkID0gR0RILmdl"
    "dFBsYXllckRhdGEocmVzWzFdLCByZXNbMl0pCiAgICAgICAgI3ByaW50KCdPYnRhaW5lZCBQbGF5"
    "ZXJkYXRhJywgbGVuKHBkKSkKICAgICAgICByZXR1cm4gX2VtKGYnL2dldHBsYXllcmRhdGEgInty"
    "ZXNbMV19IiAie3Jlc1syXX0iIHtsZW4ocGQpfScpK3BkCiAgICAjcHJpbnQoJ0FjY2VzcyBFcnJv"
    "cicsdXNyLnVzZXIubmFtZSwgJ0NhblwndCBnZXQgcGxheWVyZGF0YSBmb3InLHJlc1sxXSkKICAg"
    "IHJldHVybiBOb25lCmRlZiBfbGVhdmVnYW1lY2hhbm5lbChtZCx1c3IscmVzKToKICAgIGNobmwg"
    "PSB1c3IudXNlci5nYW1lY2hhbm5lbAogICAgaWYgY2hubDoKICAgICAgICBjaG5sLmxlYXZlQ2hh"
    "bm5lbCh1c3IpCiAgICByZXR1cm4gdXNyLnNlcnZlci5zdGF0ZS5lbnVtZXJhdGVHQygpCiMtLS0g"
    "Y29tbWFuZHMgdGFrZW4gZnJvbSB0aGUgY2xpZW50J3Mgb3duIG91dGdvaW5nIHRhYmxlIC0tLS0t"
    "LS0tLS0tLS0tLS0tLS0tLS0tCiNUaGUgZml2ZSBoYW5kbGVycyBiZWxvdyBleGlzdCBiZWNhdXNl"
    "IHRoZSBmb3JtYXQgdGFibGUgY29tcGlsZWQgaW50byB0aGUgcmV0YWlsCiNjbGllbnQgKEVOQ2xp"
    "ZW50LmNwcCwgcmVjb3ZlcmVkIGZyb20gR2FtZUhlbHBlci5kbGwgaW4gdGhlIDEuMyBTREspIGxp"
    "c3RzIHRoZW0KI2FuZCB0aGlzIHNlcnZlciBoYWQgbm8gZW50cnkgZm9yIGFueSBvZiB0aGVtLiBB"
    "biB1bnJlZ2lzdGVyZWQgY29tbWFuZCBpcyBub3QKI2lnbm9yZWQgZ3JhY2VmdWxseTogcGFyc2Uo"
    "KSBsb2dzICdVTktOT1dOIENPTU1BTkQnIGFuZCByZXR1cm5zIG5vdGhpbmcsIGFuZCBhCiNjbGll"
    "bnQgd2FpdGluZyBvbiBhbiBhbnN3ZXIgd2FpdHMgZm9yZXZlci4gVGhhdCBpcyB0aGUgc2FtZSBz"
    "aGFwZSBhcyBldmVyeSBoYW5nCiNhbHJlYWR5IHRyYWNrZWQgZG93biBpbiB0aGlzIGZpbGUuCiNU"
    "aGUgY2xpZW50IHNlbmRzLCB2ZXJiYXRpbSBmcm9tIHRoYXQgdGFibGU6CiMgICAgL2dhbWVjaGFu"
    "bmVsc2xpc3QKIyAgICAvam9pbmNoYXRjaGFubmVsICIlUyIgIiVTIiAiJWQiCiMgICAgL21zZyAi"
    "Li4uCiMgICAgL3NldGdhbWVwYXJhbXMgIiVzIiAiJXMiCiMgICAgL25ld2dhbWVob3N0ICIlcyIK"
    "ZGVmIF9nYW1lY2hhbm5lbHNsaXN0KG1kLHVzcixyZXMpOgogICAgI1BsYWluICJ3aGF0IHRvd25z"
    "IGFyZSB0aGVyZT8iLiBlbnVtZXJhdGVHQygpIGFscmVhZHkgYnVpbGRzIGV4YWN0bHkgdGhpcwog"
    "ICAgI2Fuc3dlciAtIGl0IHdhcyBvbmx5IGV2ZXIgc2VudCBhcyB0aGUgcmVwbHkgdG8gL2xlYXZl"
    "Z2FtZWNoYW5uZWwsIHNvIGEKICAgICNjbGllbnQgdGhhdCBhc2tlZCBkaXJlY3RseSBnb3Qgc2ls"
    "ZW5jZSBhbmQgYW4gZW1wdHkgdG93biBsaXN0LgogICAgcmV0dXJuIHVzci5zZXJ2ZXIuc3RhdGUu"
    "ZW51bWVyYXRlR0MoKQpkZWYgX2pvaW5jaGF0Y2hhbm5lbChtZCx1c3IscmVzKToKICAgICMoY2hh"
    "bm5lbCwgcGFzc3dvcmQsIGZsYWcpLiBqb2luQ2hhdCgpIGFscmVhZHkgcmV0dXJucyB0aGUgZnVs"
    "bCByZXBseSB0aGUKICAgICNjbGllbnQgZXhwZWN0cyAtIHRoZSBqb2luIGNvbmZpcm1hdGlvbiBw"
    "bHVzIHRoZSByb3N0ZXIgLSBhbmQgd2FzIG9ubHkKICAgICNyZWFjaGFibGUgYXMgYSBzaWRlIGVm"
    "ZmVjdCBvZiBlbnRlcmluZyBhIHRvd24sIHNvIHRoZSBzZWNvbmQgY2hhdCBjaGFubmVsCiAgICAj"
    "KFRyYWRlKSBjb3VsZCBuZXZlciBiZSBqb2luZWQ6IHRoZSBjb21tYW5kIHRvIHN3aXRjaCB3YXMg"
    "dW5oYW5kbGVkLgogICAgI1RoZSBwYXNzd29yZCBpcyBhY2NlcHRlZCBhbmQgaWdub3JlZCwgYXMg"
    "ZXZlcnl3aGVyZSBlbHNlIGluIHRoaXMgZmlsZTsgdGhlCiAgICAjdHJhaWxpbmcgaW50ZWdlcidz"
    "IG1lYW5pbmcgaXMgbm90IGtub3duIGFuZCBub3RoaW5nIGhlcmUgZGVwZW5kcyBvbiBpdC4KICAg"
    "IGNobmwgPSB1c3IudXNlci5nYW1lY2hhbm5lbAogICAgaWYgbm90IGNobmw6CiAgICAgICAgcmV0"
    "dXJuIE5vbmUgI25vdCBpbiBhIHRvd24sIG5vdGhpbmcgdG8gam9pbgogICAgbmFtZSA9IHNhbml0"
    "aXplVGV4dChyZXNbMV0sIF9NQVhfQ0hBVE5BTUUpLnN0cmlwKCkKICAgIGlmIG5vdCBuYW1lOgog"
    "ICAgICAgIHJldHVybiBOb25lCiAgICBpZiBuYW1lIG5vdCBpbiBjaG5sLmNoYXRDaGFubmVsczoK"
    "ICAgICAgICAjVGhlIGNsaWVudCBoYXMgYSAiY3JlYXRlIGNoYXQgY2hhbm5lbCIgY29udHJvbCBv"
    "ZiBpdHMgb3duCiAgICAgICAgIyhJRENfQ1JFQVRFQ0hBVENIQU5ORUwgaW4gdGhlIFNESydzIERp"
    "YWxvZ3NSZXNvdXJjZS5oKSBhbmQgbm8gc2VwYXJhdGUKICAgICAgICAjY29tbWFuZCBmb3IgaXQs"
    "IHNvIGpvaW5pbmcgYSBuYW1lIHRoYXQgZG9lcyBub3QgZXhpc3QgeWV0ICppcyogaG93IGEKICAg"
    "ICAgICAjY2hhbm5lbCBnZXRzIGNyZWF0ZWQuIFJlZnVzaW5nIGxlZnQgdGhhdCBidXR0b24gZG9p"
    "bmcgbm90aGluZyBidXQgaGFuZwogICAgICAgICN0aGUgZGlhbG9nLiBDYXBwZWQsIGJlY2F1c2Ug"
    "dGhlIG5hbWUgaXMgcGxheWVyLXN1cHBsaWVkIGFuZCB0aGVzZQogICAgICAgICNvdXRsaXZlIHRo"
    "ZSBwbGF5ZXIgd2hvIG1hZGUgdGhlbS4KICAgICAgICBpZiBsZW4oY2hubC5jaGF0Q2hhbm5lbHMp"
    "ID49IF9NQVhfQ0hBVF9DSEFOTkVMUzoKICAgICAgICAgICAgcHJpbnQoZicqKioge3Vzci51c2Vy"
    "Lm5hbWV9IGNvdWxkIG5vdCBjcmVhdGUgY2hhdCBjaGFubmVsIHtuYW1lIXJ9OiAnCiAgICAgICAg"
    "ICAgICAgICAgIGYndG93biBhbHJlYWR5IGhhcyB7bGVuKGNobmwuY2hhdENoYW5uZWxzKX0nKQog"
    "ICAgICAgICAgICByZXR1cm4gTm9uZQogICAgICAgIGNobmwuY2hhdENoYW5uZWxzW25hbWVdID0g"
    "W10KICAgICAgICBwcmludChmJ1tMb2JieV0ge3Vzci51c2VyLm5hbWV9IGNyZWF0ZWQgY2hhdCBj"
    "aGFubmVsICJ7bmFtZX0iIGluIHtjaG5sLm5hbWV9JykKICAgICAgICAjRXZlcnlvbmUgYnJvd3Np"
    "bmcgdGhlIHRvd24gZ2V0cyB0aGUgcmVmcmVzaGVkIGNoYW5uZWwgbGlzdCwgb3RoZXJ3aXNlCiAg"
    "ICAgICAgI3RoZSBuZXcgY2hhbm5lbCBpcyBpbnZpc2libGUgdG8gYWxsIGJ1dCBpdHMgY3JlYXRv"
    "ci4KICAgICAgICBtZC5hZGQoeyd0YXJnZXQnOmxpc3QoY2hubC51c2VybGlzdCksJ21lc3NhZ2Un"
    "OmNobmwuZW51bUNoYXRzKCl9KQogICAgcmV0dXJuIGNobmwuam9pbkNoYXQodXNyLCBuYW1lLCBy"
    "ZXNbMl0gaWYgbGVuKHJlcyk+MiBlbHNlICcnKQpkZWYgX21zZyhtZCx1c3IscmVzKToKICAgICNQ"
    "cml2YXRlIG1lc3NhZ2UuIFJlbGF5ZWQgaW4gdGhlIHNhbWUgc2hhcGUgL3NlbmQgdXNlcyAtICI8"
    "c2VuZGVyPiIgdGhlbiB0aGUKICAgICN0ZXh0IC0gYmVjYXVzZSB0aGF0IGlzIHRoZSBvbmUgdHdv"
    "LWZpZWxkIHRleHQgbWVzc2FnZSB0aGlzIGNsaWVudCBpcyBrbm93bgogICAgI3RvIHJlbmRlci4g"
    "VGhlIGV4YWN0IHNlcnZlci0+Y2xpZW50IHNwZWxsaW5nIGZvciBhIHByaXZhdGUgbWVzc2FnZSBo"
    "YXMgbm90CiAgICAjYmVlbiBjYXB0dXJlZDsgaWYgYSBzZXNzaW9uIGxvZyBldmVyIHNob3dzIHRo"
    "ZSBjbGllbnQgbWlzaGFuZGxpbmcgaXQsIHRoaXMKICAgICNpcyB0aGUgbGluZSB0byByZXZpc2l0"
    "LiBEb2luZyBub3RoaW5nIHdhcyBub3QgdGhlIHNhZmVyIG9wdGlvbjogaXQgaXMgd2hhdAogICAg"
    "I3RoZSBzZXJ2ZXIgZGlkIHVudGlsIG5vdywgYW5kIHByaXZhdGUgbWVzc2FnZXMgc2ltcGx5IHZh"
    "bmlzaGVkLgogICAgaWYgbGVuKHJlcyk8MzoKICAgICAgICByZXR1cm4gTm9uZQogICAgdGFyZ2V0"
    "ID0gcmVzWzFdCiAgICB0ZXh0ID0gc2FuaXRpemVUZXh0KHJlc1syXSwgX01BWF9DSEFUX1RFWFQp"
    "CiAgICBpZiBub3QgdGV4dDoKICAgICAgICByZXR1cm4gTm9uZQogICAgdGNvbiA9IHVzci5zZXJ2"
    "ZXIuZ2V0UGxheWVyKHRhcmdldCkKICAgIGlmIHRjb24gaXMgTm9uZToKICAgICAgICByZXR1cm4g"
    "Tm9uZSAjcmVjaXBpZW50IG9mZmxpbmUKICAgIHRjb24uc2VuZChfZW0oZicvbXNnICJ7dXNyLnVz"
    "ZXIubmFtZX0iICJ7dGV4dH0iJykpCiAgICByZXR1cm4gTm9uZQpkZWYgX3NldGdhbWVwYXJhbXMo"
    "bWQsdXNyLHJlcyk6CiAgICAjVGhlIHJvb20ncyBvd24gdHdvIGRlc2NyaXB0aXZlIGZpZWxkcywg"
    "cmUtc2VudCBhZnRlciB0aGUgcm9vbSBleGlzdHM6IHRoZQogICAgI3BhcmFtZXRlciBzdHJpbmcg"
    "YW5kIGl0cyB0cmFuc2xhdGUgaWQsIHRoZSB0aGlyZCBhbmQgZm91cnRoIGZpZWxkcyBvZgogICAg"
    "IyRnYW1lLiBUaGVpciBtZWFuaW5nIGlzIG5vdCBkb2N1bWVudGVkLCBzbyB0aGV5IGFyZSBub3Qg"
    "dGFrZW4gb24gdHJ1c3QgLQogICAgI2EgcGFpciBvbmx5IHJlcGxhY2VzIHRoZSByb29tJ3Mgd2hl"
    "biBpdCBpcyBzaGFwZWQgbGlrZSBvbmUgKHNlZQogICAgI19pc1Jvb21QYXJhbXMpLiBBbnl0aGlu"
    "ZyBlbHNlIGlzIGxvZ2dlZCBhbmQgbGVmdCBhbG9uZSwgd2hpY2ggaXMgd2hhdCB0aGlzCiAgICAj"
    "aGFuZGxlciB1c2VkIHRvIGRvIHdpdGggZXZlcnkgY2FsbC4KICAgICMKICAgICNBcHBseWluZyB0"
    "aGVtIG1hdHRlcnMgYmVjYXVzZSB0aGUgbW9kZSBzbG90IGluc2lkZSB0aGUgcGFyYW1ldGVyIHN0"
    "cmluZwogICAgI2lzIHdoZXJlIGEgY28tb3AgZGlmZmljdWx0eSBsaXZlcyAobW9kcy9jb29wLWVh"
    "c3kgcHV0cyBWZXJ5TG93L0xvdy9IYXJkCiAgICAjdGhlcmUpLiBBIGpvaW5lciBidWlsZHMgaXRz"
    "IG1pc3Npb24gZnJvbSB0aGUgcGFyYW1ldGVycyB0aGUgbG9iYnkgbGFzdAogICAgI2Fubm91bmNl"
    "ZCwgc28gZHJvcHBpbmcgYSBjaGFuZ2UgdGhlIGhvc3QgbWFkZSBsZWF2ZXMgdGhlIHR3byBwbGF5"
    "ZXJzCiAgICAjZ2VuZXJhdGluZyBkaWZmZXJlbnQgd29ybGRzIC0gZGlmZmVyZW50IGVuZW1pZXMs"
    "IGF0IGRpZmZlcmVudCBsZXZlbHMsCiAgICAjY2FycnlpbmcgZGlmZmVyZW50IGdlYXIgLSBmcm9t"
    "IHdoYXQgdGhleSBib3RoIGJlbGlldmUgaXMgb25lIHJvb20uCiAgICBnbSA9IHVzci51c2VyLmdh"
    "bWUKICAgIGlmIGdtIGlzIE5vbmUgb3IgZ20uaG9zdCBpcyBub3QgdXNyOgogICAgICAgIHJldHVy"
    "biBOb25lICNvbmx5IHRoZSByb29tJ3Mgb3duIGhvc3QgbWF5IHRvdWNoIGl0cyBwYXJhbWV0ZXJz"
    "CiAgICBwYXIgPSByZXNbMV0uc3RyaXAoKQogICAgdHJhbiA9IHJlc1syXS5zdHJpcCgpCiAgICBp"
    "ZiBfaXNSb29tUGFyYW1zKHBhcikgYW5kIF9SRV9ST09NX0lELm1hdGNoKHRyYW4pOgogICAgICAg"
    "IHByaW50KGYnW0xvYmJ5XSB7dXNyLnVzZXIubmFtZX0gc2V0ICJ7Z20uZ25hbWV9IiB0byB7cGFy"
    "IXJ9IHt0cmFuIXJ9ICcKICAgICAgICAgICAgICBmJyh3YXMge2dtLm1hcFBhciFyfSB7Z20ubWFw"
    "VHJhbnNsYXRlIXJ9KScpCiAgICAgICAgZ20ubWFwUGFyID0gcGFyCiAgICAgICAgZ20ubWFwVHJh"
    "bnNsYXRlID0gdHJhbgogICAgZWxzZToKICAgICAgICBwcmludChmJ1tMb2JieV0ge3Vzci51c2Vy"
    "Lm5hbWV9IC9zZXRnYW1lcGFyYW1zIGZvciAie2dtLmduYW1lfSI6ICcKICAgICAgICAgICAgICBm"
    "J3tyZXNbMV0hcn0ge3Jlc1syXSFyfSAtIG5vdCBhIHJvb20gZGVzY3JpcHRpb24sIGxlZnQgYXMg"
    "dGhleSB3ZXJlJykKICAgIG1zZyA9IGdtLmdldEdhbWVTdHJpbmcoKQogICAgaWYgbXNnOgogICAg"
    "ICAgIG1kLmFkZCh7J3RhcmdldCc6Z20uX2F1ZGllbmNlKCksJ21lc3NhZ2UnOm1zZ30pCiAgICBy"
    "ZXR1cm4gTm9uZQpkZWYgX25ld2dhbWVob3N0KG1kLHVzcixyZXMpOgogICAgI0EgZnJlc2ggeC1k"
    "aXJlY3RwbGF5IFVSTCBmb3IgYSByb29tIHRoYXQgYWxyZWFkeSBleGlzdHMuIEl0IGNhcnJpZXMg"
    "dGhlCiAgICAjaG9zdCdzIG93biBpZGVhIG9mIGl0cyBhZGRyZXNzLCB3aGljaCBiZWhpbmQgYSBy"
    "b3V0ZXIgaXMgYSBMQU4gYWRkcmVzcyBubwogICAgI2pvaW5lciBjYW4gcmVhY2ggLSB0aGUgc2Ft"
    "ZSBwcm9ibGVtIC9jcmVhdGVnYW1lIGhhcywgYW5kIGl0IG11c3QgZ2V0IHRoZQogICAgI3NhbWUg"
    "dHJlYXRtZW50LCBvciBhIHJvb20gd2hvc2UgaG9zdCByZS1hZHZlcnRpc2VzIHNpbGVudGx5IGJl"
    "Y29tZXMKICAgICN1bmpvaW5hYmxlIHdoaWxlIHN0aWxsIGJlaW5nIGxpc3RlZC4KICAgIGdtID0g"
    "dXNyLnVzZXIuZ2FtZQogICAgaWYgZ20gaXMgTm9uZSBvciBnbS5ob3N0IGlzIG5vdCB1c3I6CiAg"
    "ICAgICAgcmV0dXJuIE5vbmUgI29ubHkgdGhlIGhvc3QgZGVzY3JpYmVzIHdoZXJlIHRoZSBnYW1l"
    "IGlzCiAgICBwZWVyID0gdXNyLmNsaWVudF9hZGRyZXNzWzBdIGlmIHVzci5jbGllbnRfYWRkcmVz"
    "cyBlbHNlICcnCiAgICAodXJsLCBub3RlKSA9IHJld3JpdGVHYW1lSG9zdChyZXNbMV0sIHBlZXIp"
    "CiAgICBnbS51cmwgPSB1cmwKICAgIHByaW50KGYnW0xvYmJ5XSB7dXNyLnVzZXIubmFtZX0gbW92"
    "ZWQgcm9vbSAie2dtLmduYW1lfSI6IHtub3RlfScpCiAgICBwcmludChmJ1tMb2JieV0gICB1cmwg"
    "YWR2ZXJ0aXNlZCB0byBqb2luZXJzOiB7Z20udXJsfScpCiAgICBtc2cgPSBnbS5nZXRHYW1lU3Ry"
    "aW5nKCkKICAgIGlmIG1zZzoKICAgICAgICBtZC5hZGQoeyd0YXJnZXQnOmdtLl9hdWRpZW5jZSgp"
    "LCdtZXNzYWdlJzptc2d9KQogICAgcmV0dXJuIE5vbmUKZGVmIF9yZXF1ZXN0am9pbmdhbWVjaGFu"
    "bmVsKG1kLHVzcixyZXMpOgogICAgY2hubCA9IHVzci5zZXJ2ZXIuc3RhdGUuZ2FtZUNoYW5uZWxz"
    "LmdldChyZXNbMV0pCiAgICBpZiBjaG5sIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIF9lbShmJy9y"
    "ZXF1ZXN0am9pbmdhbWVjaGFubmVsICJ7cmVzWzFdfSIgIjAiJykgI3Vua25vd24gY2hhbm5lbAog"
    "ICAgI1RPRE8gY2hlY2sgcGVybWlzc2lvbnM/CiAgICBpZiBjaG5sLnJlcXVlc3RKb2luKHVzcik6"
    "CiAgICAgICAgcmV0dXJuIF9lbShmJy9yZXF1ZXN0am9pbmdhbWVjaGFubmVsICJ7cmVzWzFdfSIg"
    "IjEiJykKICAgIHJldHVybiBfZW0oZicvcmVxdWVzdGpvaW5nYW1lY2hhbm5lbCAie3Jlc1sxXX0i"
    "ICIwIicpCmRlZiBfam9pbmdhbWVjaGFubmVsKG1kLHVzcixyZXMpOgogICAgY2hubCA9IHVzci5z"
    "ZXJ2ZXIuc3RhdGUuZ2FtZUNoYW5uZWxzLmdldChyZXNbMV0pCiAgICBpZiBjaG5sIGlzIE5vbmU6"
    "CiAgICAgICAgcmV0dXJuIE5vbmUgI3Vua25vd24gY2hhbm5lbCwgaWdub3JlCiAgICBpZiBsZW4o"
    "cmVzKT4yOgogICAgICAgIHBvcyA9IF9oZXJvUG9zKHJlc1syXSkKICAgICAgICBpZiBwb3MgaXMg"
    "bm90IE5vbmU6CiAgICAgICAgICAgIHVzci51c2VyLnBvc2RhdGEgPSBwb3MKICAgIHJldHVybiBj"
    "aG5sLmpvaW5DaGFubmVsKHVzciwgcmVzWzFdKQpkZWYgX3NldHVzZXJoZXJvZGF0YShtZCx1c3Is"
    "cmVzKToKICAgIHBkID0gX1JlYWRCbG9iKHVzciwgcmVzWzJdKQogICAgaWYgbGVuKHBkKSA+IF9N"
    "QVhfSEVST0RBVEE6CiAgICAgICAgI1VubGlrZSAvc2V0cGxheWVyZGF0YSwgd2hpY2ggaXMgd3Jp"
    "dHRlbiB0byBkaXNrIGFuZCByZWFkIGJhY2sgYnkgaXRzCiAgICAgICAgI293bmVyIGFsb25lLCBo"
    "ZXJvZGF0YSBpcyByZS1icm9hZGNhc3QgdG8gZXZlcnkgb3RoZXIgcGxheWVyIGluIHRoZSB0b3du"
    "CiAgICAgICAgI29uIGV2ZXJ5IGpvaW4gYW5kIG9uIGV2ZXJ5IGNoYW5nZS4gQXQgdGhlIGdlbmVy"
    "YWwgX01BWF9CTE9CIGNlaWxpbmcgb25lCiAgICAgICAgI2NsaWVudCBjb3VsZCBoYW5kIHRoZSBz"
    "ZXJ2ZXIgMTYgTUIgYW5kIGhhdmUgaXQgZmFubmVkIG91dCBmaWZ0eSB0aW1lcywKICAgICAgICAj"
    "d2hpY2ggYmxvd3MgcGFzdCBldmVyeSByZWNpcGllbnQncyBzZW5kLWJhY2tsb2cgY2FwIGFuZCBk"
    "cm9wcyB0aGUgd2hvbGUKICAgICAgICAjdG93biBpbnN0ZWFkIG9mIHRoZSBjbGllbnQgdGhhdCBk"
    "aWQgaXQuIFJlYWwgaGVybyBhcHBlYXJhbmNlIGRhdGEgaXMgYQogICAgICAgICNmZXcga2lsb2J5"
    "dGVzLgogICAgICAgIHJhaXNlIFByb3RvY29sRXJyb3IoZidoZXJvZGF0YSBibG9iIG9mIHtsZW4o"
    "cGQpfSBieXRlcyBleGNlZWRzICcKICAgICAgICAgICAgICAgICAgICAgICAgICAgIGYne19NQVhf"
    "SEVST0RBVEF9JykKICAgIHVzci51c2VyLmhlcm9kYXRhID0gcGQKICAgIGlmIHVzci51c2VyLmdh"
    "bWVjaGFubmVsOgogICAgICAgIG1zZyA9IHVzci51c2VyLmdldEdDVW1zZygpCiAgICAgICAgdGcg"
    "PSBfd29Vc2VyKHVzci51c2VyLmdhbWVjaGFubmVsLnVzZXJsaXN0LCB1c3IpCiAgICAgICAgbWQu"
    "YWRkKHsndGFyZ2V0Jzp0ZywnbWVzc2FnZSc6bXNnfSkKICAgIHJldHVybiBOb25lCmRlZiBfc2Vu"
    "ZChtZCx1c3IscmVzKToKICAgIGlmIG5vdCB1c3IudXNlci5jaGF0Y2hhbm5lbDoKICAgICAgICBy"
    "ZXR1cm4gTm9uZQogICAgaWYgbGVuKHJlcyk8MjoKICAgICAgICByZXR1cm4gTm9uZQogICAgdGV4"
    "dCA9IHNhbml0aXplVGV4dChyZXNbMV0sIF9NQVhfQ0hBVF9URVhUKQogICAgaWYgbm90IHRleHQ6"
    "CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGlmIF9BRE1JTlMgYW5kIHRleHQuc3RhcnRzd2l0aChf"
    "QURNSU5fUFJFRklYKToKICAgICAgICAjTmV2ZXIgcmVsYXllZCB0byB0aGUgY2hhbm5lbCwgd2hv"
    "ZXZlciB0eXBlZCBpdC4gRm9yIGFuIGFkbWluIHRoYXQKICAgICAgICAja2VlcHMgdGhlIHNlcnZl"
    "cidzIGJ1c2luZXNzIG9mZiB0aGUgcHVibGljIGNoYXQ7IGZvciBldmVyeWJvZHkgZWxzZSBpdAog"
    "ICAgICAgICNzdG9wcyB0aGUgcm9vbSBsZWFybmluZyB3aGljaCBjb21tYW5kcyBleGlzdCBieSB3"
    "YXRjaGluZyBzb21lb25lIGd1ZXNzCiAgICAgICAgI2F0IHRoZW0uCiAgICAgICAgI1RoZSBgX0FE"
    "TUlOUyBhbmRgIGd1YXJkIG1hdHRlcnM6IHdpdGggbm8gYWRtaW5zIGNvbmZpZ3VyZWQgdGhlIGNv"
    "bnNvbGUKICAgICAgICAjaXMgbWVhbnQgdG8gYmUgb2ZmIG91dHJpZ2h0LCBidXQgdGhpcyBicmFu"
    "Y2ggc3RpbGwgYXRlIGV2ZXJ5IGNoYXQgbGluZQogICAgICAgICN0aGF0IGhhcHBlbmVkIHRvIHN0"
    "YXJ0IHdpdGggJyEnIC0gc28gb24gYSBkZWZhdWx0IHNlcnZlciAiISEhIiBvcgogICAgICAgICMi"
    "IdGD0YDQsCIgc2ltcGx5IG5ldmVyIHJlYWNoZWQgdGhlIHJvb20sIHdpdGggbm90aGluZyBvbiBz"
    "Y3JlZW4gdG8gc2F5CiAgICAgICAgI3doeS4gV2l0aCBubyBhZG1pbnMgdGhlcmUgaXMgbm8gY29u"
    "c29sZSwgc28gdGhlcmUgaXMgbm90aGluZyB0byBoaWRlCiAgICAgICAgI2FuZCB0aGUgbGluZSBp"
    "cyBvcmRpbmFyeSBjaGF0LgogICAgICAgIHJldHVybiBhZG1pbkNvbW1hbmQodXNyLCB0ZXh0W2xl"
    "bihfQURNSU5fUFJFRklYKTpdLnN0cmlwKCkpCiAgICB1bCA9IHVzci51c2VyLmNoYXRjaGFubmVs"
    "CiAgICBtZC5hZGQoeyd0YXJnZXQnOnVsLCdtZXNzYWdlJzpfZW0oZicvc2VuZCAie3Vzci51c2Vy"
    "Lm5hbWV9IiAie3RleHR9IicpfSkKICAgIHJldHVybiBOb25lCiMtLS0gaW4tZ2FtZSBhZG1pbiBj"
    "b25zb2xlIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t"
    "LS0tCiNUeXBlZCBpbnRvIHRoZSBnYW1lJ3Mgb3duIGNoYXQgYm94LCBzbyBpdCBuZWVkcyBubyBj"
    "bGllbnQgbW9kaWZpY2F0aW9uIGF0IGFsbDoKI3RoZSByZXRhaWwgY2xpZW50IGFscmVhZHkgc2Vu"
    "ZHMgZXZlcnl0aGluZyB0eXBlZCB0aGVyZSBhcyAvc2VuZCwgYW5kIGFscmVhZHkKI3JlbmRlcnMg"
    "Jy9hZG1pbiA8dGV4dD4nIGNvbWluZyBiYWNrIHRoZSBvdGhlciB3YXkgKHRoYXQgaXMgaG93IGEg"
    "a2ljayBub3RpY2UKI3JlYWNoZXMgYSBwbGF5ZXIpLiBCb3RoIGhhbHZlcyBhcmUgdGhlcmVmb3Jl"
    "IGtub3duLWdvb2QgbWVzc2FnZSBzaGFwZXMsIHdoaWNoCiNpcyB3aGF0IG1ha2VzIHRoaXMgc2Fm"
    "ZSBvbiBhIDIwMDggYmluYXJ5IC0gbm90aGluZyBuZXcgaXMgaW52ZW50ZWQgb24gdGhlIHdpcmUu"
    "CiNPbmx5IGFjY291bnRzIGxpc3RlZCBhcyBBZG1pbnMgaW4gQ29uZmlnLmluaSBhcmUgb2JleWVk"
    "LiBFdmVyeW9uZSBlbHNlJ3MKI2NvbW1hbmRzIGFyZSBzd2FsbG93ZWQgc2lsZW50bHkgcmF0aGVy"
    "IHRoYW4gYW5zd2VyZWQsIHNvIHRoZSBwcmVzZW5jZSBvZiB0aGUKI2NvbnNvbGUgaXMgbm90IGFk"
    "dmVydGlzZWQgdG8gdGhlIHJvb20uCmRlZiBfYWRtaW5SZXBseSh1c3IsIGxpbmVzKToKICAgICNP"
    "bmUgL2FkbWluIHBlciBsaW5lOiB0aGUgY2xpZW50IHRyZWF0cyBlYWNoIGFzIGl0cyBvd24gc2Vy"
    "dmVyIG1lc3NhZ2UsIGFuZAogICAgI2Egc2luZ2xlIGxvbmcgbGluZSB3b3VsZCBydW4gaW50byB0"
    "aGUgd2lyZS1sZW5ndGggbGltaXQgYW55d2F5LgogICAgb3V0ID0gYicnCiAgICBmb3IgbGluZSBp"
    "biBsaW5lczoKICAgICAgICBvdXQgKz0gX2VtKGYnL2FkbWluIHtzYW5pdGl6ZVRleHQoc3RyKGxp"
    "bmUpLCBfTUFYX0NIQVRfVEVYVCl9JykKICAgIHJldHVybiBvdXQgb3IgTm9uZQpkZWYgX2ZtdFBs"
    "YXllcnMoc2VydmVyKToKICAgIHJvd3MgPSBbXQogICAgZm9yIChuYW1lLCBjb24pIGluIHNvcnRl"
    "ZChzZXJ2ZXIuc3RhdGUuYWN0aXZlVXNlcnMuaXRlbXMoKSk6CiAgICAgICAgdG93biA9IGNvbi51"
    "c2VyLmdhbWVjaGFubmVsLm5hbWUuc3BsaXQoJyMnKVswXSBpZiBjb24udXNlci5nYW1lY2hhbm5l"
    "bCBlbHNlICctJwogICAgICAgIGdhbWUgPSBjb24udXNlci5nYW1lLmduYW1lIGlmIGNvbi51c2Vy"
    "LmdhbWUgZWxzZSAnLScKICAgICAgICByb3dzLmFwcGVuZChmJ3tuYW1lfSAgdG93bjp7dG93bn0g"
    "IHJvb206e2dhbWV9JykKICAgIHJldHVybiByb3dzIG9yIFsnbm9ib2R5IG9ubGluZSddCmRlZiBh"
    "ZG1pbkNvbW1hbmQodXNyLCBsaW5lKToKICAgIHNlcnZlciA9IHVzci5zZXJ2ZXIKICAgIHdobyA9"
    "IHVzci51c2VyLm5hbWUKICAgIGlmIHdoby5jYXNlZm9sZCgpIG5vdCBpbiBfQURNSU5TOgogICAg"
    "ICAgIHByaW50KGYnW0xvYmJ5XSB7d2hvfSB0cmllZCBhbiBhZG1pbiBjb21tYW5kIHdpdGhvdXQg"
    "YmVpbmcgYW4gYWRtaW46IHtsaW5lIXJ9JykKICAgICAgICByZXR1cm4gTm9uZQogICAgcGFydHMg"
    "PSBsaW5lLnNwbGl0KE5vbmUsIDEpCiAgICBjbWQgPSBwYXJ0c1swXS5sb3dlcigpIGlmIHBhcnRz"
    "IGVsc2UgJycKICAgIGFyZyA9IHBhcnRzWzFdLnN0cmlwKCkgaWYgbGVuKHBhcnRzKSA+IDEgZWxz"
    "ZSAnJwogICAgcHJpbnQoZidbTG9iYnldIEFETUlOIHt3aG99OiB7bGluZSFyfScpCiAgICBnbG9i"
    "YWwgX1BPU19VUERBVEVfSFosIF9JRExFX1RJTUVPVVQsIF9TRU5EX05PUFMsIERFRkFVTFRfTU9U"
    "RAogICAgaWYgY21kIGluICgnaGVscCcsICc/JywgJycpOgogICAgICAgIHJldHVybiBfYWRtaW5S"
    "ZXBseSh1c3IsIFsKICAgICAgICAgICAgZid7X0FETUlOX1BSRUZJWH13aG8gLSB3aG8gaXMgb25s"
    "aW5lJywKICAgICAgICAgICAgZid7X0FETUlOX1BSRUZJWH1zYXkgPHRleHQ+IC0gYW5ub3VuY2Ug"
    "dG8gZXZlcnlvbmUnLAogICAgICAgICAgICBmJ3tfQURNSU5fUFJFRklYfWtpY2sgPG5hbWU+JywK"
    "ICAgICAgICAgICAgZid7X0FETUlOX1BSRUZJWH1tb3RkIDx0ZXh0PicsCiAgICAgICAgICAgIGYn"
    "e19BRE1JTl9QUkVGSVh9aHogPDAuNS17X1BPU19VUERBVEVfSFpfTUFYfT4gLSBwb3NpdGlvbiBz"
    "eW5jIHJhdGUnLAogICAgICAgICAgICBmJ3tfQURNSU5fUFJFRklYfWlkbGUgPHNlY29uZHMsIDA9"
    "b2ZmPicsCiAgICAgICAgICAgIGYne19BRE1JTl9QUkVGSVh9a2VlcGFsaXZlIG9ufG9mZicsCiAg"
    "ICAgICAgICAgIGYne19BRE1JTl9QUkVGSVh9c3RhdHVzJywKICAgICAgICAgICAgZid7X0FETUlO"
    "X1BSRUZJWH1zYXZlIC0gd3JpdGUgdGhlc2Ugc2V0dGluZ3MgdG8gQ29uZmlnLmluaScsCiAgICAg"
    "ICAgXSkKICAgIGlmIGNtZCA9PSAnd2hvJzoKICAgICAgICByZXR1cm4gX2FkbWluUmVwbHkodXNy"
    "LCBfZm10UGxheWVycyhzZXJ2ZXIpKQogICAgaWYgY21kID09ICdzdGF0dXMnOgogICAgICAgIHJl"
    "dHVybiBfYWRtaW5SZXBseSh1c3IsIFsKICAgICAgICAgICAgZidwbGF5ZXJzIHtsZW4oc2VydmVy"
    "LnN0YXRlLmFjdGl2ZVVzZXJzKX0sICcKICAgICAgICAgICAgZidoeiB7X1BPU19VUERBVEVfSFp9"
    "LCBpZGxlIHtfSURMRV9USU1FT1VUfXMsICcKICAgICAgICAgICAgZidrZWVwYWxpdmUgeyJvbiIg"
    "aWYgX1NFTkRfTk9QUyBlbHNlICJvZmYifScsCiAgICAgICAgXSkKICAgIGlmIGNtZCA9PSAnc2F5"
    "JzoKICAgICAgICBpZiBub3QgYXJnOgogICAgICAgICAgICByZXR1cm4gX2FkbWluUmVwbHkodXNy"
    "LCBbJ3NheSB3aGF0PyddKQogICAgICAgIG1zZyA9IF9lbShmJy9hZG1pbiB7c2FuaXRpemVUZXh0"
    "KGFyZywgX01BWF9DSEFUX1RFWFQpfScpCiAgICAgICAgc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0"
    "JzpsaXN0KHNlcnZlci5zdGF0ZS5hY3RpdmVVc2Vycy52YWx1ZXMoKSksJ21lc3NhZ2UnOm1zZ30p"
    "CiAgICAgICAgcmV0dXJuIE5vbmUgI3RoZSBhbm5vdW5jZW1lbnQgaXRzZWxmIGlzIHRoZSBhZG1p"
    "bidzIGNvbmZpcm1hdGlvbgogICAgaWYgY21kID09ICdraWNrJzoKICAgICAgICBpZiBub3QgYXJn"
    "OgogICAgICAgICAgICByZXR1cm4gX2FkbWluUmVwbHkodXNyLCBbJ2tpY2sgd2hvPyddKQogICAg"
    "ICAgIGlmIGFyZy5jYXNlZm9sZCgpID09IHdoby5jYXNlZm9sZCgpOgogICAgICAgICAgICByZXR1"
    "cm4gX2FkbWluUmVwbHkodXNyLCBbJ2tpY2tpbmcgeW91cnNlbGYgaXMgbm90IGEgcGxhbiddKQog"
    "ICAgICAgIG9rID0gc2VydmVyLmtpY2tQbGF5ZXIoYXJnLCBmJ0tpY2tlZCBieSB7d2hvfScpCiAg"
    "ICAgICAgcmV0dXJuIF9hZG1pblJlcGx5KHVzciwgW2Yna2lja2VkIHthcmd9JyBpZiBvayBlbHNl"
    "IGYne2FyZ30gaXMgbm90IG9ubGluZSddKQogICAgaWYgY21kID09ICdtb3RkJzoKICAgICAgICBp"
    "ZiBub3QgYXJnOgogICAgICAgICAgICByZXR1cm4gX2FkbWluUmVwbHkodXNyLCBbJ21vdGQgbmVl"
    "ZHMgc29tZSB0ZXh0J10pCiAgICAgICAgREVGQVVMVF9NT1REID0gYXJnCiAgICAgICAgcmV0dXJu"
    "IF9hZG1pblJlcGx5KHVzciwgWydtb3RkIHNldCAoc2hvd24gYXQgdGhlIG5leHQgbG9naW4pJ10p"
    "CiAgICBpZiBjbWQgPT0gJ2h6JzoKICAgICAgICB0cnk6CiAgICAgICAgICAgIGh6ID0gZmxvYXQo"
    "YXJnKQogICAgICAgIGV4Y2VwdCBWYWx1ZUVycm9yOgogICAgICAgICAgICByZXR1cm4gX2FkbWlu"
    "UmVwbHkodXNyLCBbJ2h6IG5lZWRzIGEgbnVtYmVyJ10pCiAgICAgICAgI0NsYW1wZWQgZXhhY3Rs"
    "eSBhcyBhcHBseUNvbmZpZygpIGRvZXMgLSBvbmUgcnVsZSwgb25lIHBsYWNlIHRvIGNoYW5nZS4K"
    "ICAgICAgICBfUE9TX1VQREFURV9IWiA9IG1pbihtYXgoaHosIDAuNSksIF9QT1NfVVBEQVRFX0ha"
    "X01BWCkKICAgICAgICByZXR1cm4gX2FkbWluUmVwbHkodXNyLCBbZidwb3NpdGlvbiBzeW5jIG5v"
    "dyB7X1BPU19VUERBVEVfSFp9L3MnXSkKICAgIGlmIGNtZCA9PSAnaWRsZSc6CiAgICAgICAgdHJ5"
    "OgogICAgICAgICAgICBfSURMRV9USU1FT1VUID0gbWF4KDAsIGludChhcmcpKQogICAgICAgIGV4"
    "Y2VwdCBWYWx1ZUVycm9yOgogICAgICAgICAgICByZXR1cm4gX2FkbWluUmVwbHkodXNyLCBbJ2lk"
    "bGUgbmVlZHMgYSB3aG9sZSBudW1iZXIgb2Ygc2Vjb25kcyddKQogICAgICAgIHJldHVybiBfYWRt"
    "aW5SZXBseSh1c3IsIFtmJ2lkbGUgdGltZW91dCBub3cge19JRExFX1RJTUVPVVR9cyddKQogICAg"
    "aWYgY21kID09ICdrZWVwYWxpdmUnOgogICAgICAgIGlmIGFyZy5sb3dlcigpIG5vdCBpbiAoJ29u"
    "JywgJ29mZicpOgogICAgICAgICAgICByZXR1cm4gX2FkbWluUmVwbHkodXNyLCBbJ2tlZXBhbGl2"
    "ZSBvbnxvZmYnXSkKICAgICAgICBfU0VORF9OT1BTID0gYXJnLmxvd2VyKCkgPT0gJ29uJwogICAg"
    "ICAgIHJldHVybiBfYWRtaW5SZXBseSh1c3IsIFtmJ2tlZXBhbGl2ZSB7Im9uIiBpZiBfU0VORF9O"
    "T1BTIGVsc2UgIm9mZiJ9J10pCiAgICBpZiBjbWQgPT0gJ3NhdmUnOgogICAgICAgICNFdmVyeXRo"
    "aW5nIGFib3ZlIGNoYW5nZXMgdGhlIGxpdmUgc2VydmVyIG9ubHkuIFRoaXMgaXMgdGhlIG9uZSBj"
    "b21tYW5kCiAgICAgICAgI3RoYXQgdG91Y2hlcyB0aGUgZmlsZSwgc28gYSBzZXNzaW9uIG9mIGV4"
    "cGVyaW1lbnRzIGNhbm5vdCBiZSBtYWRlCiAgICAgICAgI3Blcm1hbmVudCBieSBhY2NpZGVudC4K"
    "ICAgICAgICBjZmcgPSBsb2FkQ29uZmlnKCkKICAgICAgICBzZWMgPSBjZmdbJ3NlcnZlciddCiAg"
    "ICAgICAgc2VjWydNT1REJ10gPSBfZXNjYXBlTU9URChERUZBVUxUX01PVEQpCiAgICAgICAgc2Vj"
    "WydQb3NpdGlvblVwZGF0ZUh6J10gPSBzdHIoX1BPU19VUERBVEVfSFopCiAgICAgICAgc2VjWydJ"
    "ZGxlVGltZW91dCddID0gc3RyKF9JRExFX1RJTUVPVVQpCiAgICAgICAgc2VjWydLZWVwYWxpdmUn"
    "XSA9IHN0cihfU0VORF9OT1BTKQogICAgICAgIHNhdmVDb25maWcoY2ZnKQogICAgICAgIHJldHVy"
    "biBfYWRtaW5SZXBseSh1c3IsIFsnc2F2ZWQgdG8gQ29uZmlnLmluaSddKQogICAgcmV0dXJuIF9h"
    "ZG1pblJlcGx5KHVzciwgW2YndW5rbm93biBjb21tYW5kIHtjbWQhcn0gLSB0cnkge19BRE1JTl9Q"
    "UkVGSVh9aGVscCddKQpkZWYgX2dldGd1aWxkcmFua3BvaW50cyhtZCx1c3IscmVzKToKICAgIChh"
    "LGIsYyxkKSA9IF9ncnAoKQogICAgcmV0dXJuIF9lbShmJy9nZXRndWlsZHJhbmtwb2ludHMgInth"
    "fSIgIntifSIgIntjfSIgIntkfSInKQoKIyMgR1VJTERTCiNHdWlsZCBjcmVhdGlvbiBkaWQgbm90"
    "aGluZyBhdCBhbGwgYmVmb3JlIHRoaXM6IHRoZXJlIHdhcyBubyAvY3JlYXRlZ3VpbGQgKG9yCiNh"
    "bnl0aGluZyBlbHNlIGd1aWxkLXJlbGF0ZWQpIGluIF9DT01NQU5EUywgc28gdGhlIGNsaWVudCdz"
    "IHJlcXVlc3QgZmVsbAojdGhyb3VnaCB0byB0aGUgIlVua25vd24gQ29tbWFuZCIgYnJhbmNoIG9m"
    "IENvbW1hbmRQYXJzZXIucGFyc2UgYW5kIHdhcwojZHJvcHBlZC4gVGhlIGNsaWVudCBnb3Qgbm8g"
    "cmVwbHksIG5vIGVycm9yLCBhbmQgbm8gZ3VpbGQuCiNOT1RFIE9OIENPTU1BTkQgTkFNRVM6IHRo"
    "ZSBleGFjdCB3aXJlIG5hbWVzIHRoZSByZXRhaWwgY2xpZW50IHVzZXMgZm9yIHRoZQojZ3VpbGQg"
    "VUkgYXJlIG5vdCBkb2N1bWVudGVkIGFueXdoZXJlIHdlIGhhdmUuIFRoZSBoYW5kbGVycyBiZWxv"
    "dyBhcmUKI3JlZ2lzdGVyZWQgdW5kZXIgZXZlcnkgc3BlbGxpbmcgdGhhdCBmaXRzIHRoaXMgcHJv"
    "dG9jb2wncyBjb252ZW50aW9ucywgYWxsCiNyb3V0ZWQgdG8gdGhlIHNhbWUgaW1wbGVtZW50YXRp"
    "b24sIHNvIHdoaWNoZXZlciBvbmUgdGhlIGNsaWVudCBhY3R1YWxseQojc2VuZHMgaXMgc2VydmVk"
    "LiBwYXJzZSgpIG5vdyBsb2dzIHRoZSByYXcgdGV4dCBvZiBhbnl0aGluZyBzdGlsbCB1bm1hdGNo"
    "ZWQsCiN3aGljaCBpcyBob3cgdG8gY29uZmlybS90cmltIHRoaXMgbGlzdCBmcm9tIGEgcmVhbCBz"
    "ZXNzaW9uJ3MgbG9nLgpkZWYgX3Rlc3RjcmVhdGVndWlsZChtZCx1c3IscmVzKToKICAgICNDb25m"
    "aXJtZWQgZnJvbSBhIGxpdmUgY2xpZW50IGNhcHR1cmU6IG9wZW5pbmcgdGhlIGd1aWxkIHNjcmVl"
    "biBzZW5kcwogICAgIy9ndWlsZHNsYWRkZXIsIGFuZCB0eXBpbmcgYSBuYW1lIGFuZCBwcmVzc2lu"
    "ZyBjcmVhdGUgc2VuZHMKICAgICMvdGVzdGNyZWF0ZWd1aWxkICI8bmFtZT4iLiBUaGUgY2xpZW50"
    "IHRoZW4gd2FpdHMgZm9yIHRoZSBzZXJ2ZXIgdG8gc2F5CiAgICAjd2hldGhlciB0aGF0IG5hbWUg"
    "Y2FuIGJlIHVzZWQgLSB3aXRoIG5vIGFuc3dlciBpdCB3YWl0cyBmb3JldmVyLCB3aGljaCBpcwog"
    "ICAgI3doYXQgdGhlICJndWlsZCBjcmVhdGlvbiBoYW5ncyIgcmVwb3J0IHdhcy4gRXZlcnkgZ3Vp"
    "bGQgY29tbWFuZCBuYW1lCiAgICAjZ3Vlc3NlZCBiZWZvcmUgdGhpcyBjYXB0dXJlICggL2NyZWF0"
    "ZWd1aWxkLCAvam9pbmd1aWxkLCAuLi4gKSB3YXMgd3Jvbmc7CiAgICAjdGhpcyBvbmUgY29tZXMg"
    "ZnJvbSB0aGUgd2lyZS4KICAgIG5hbWUgPSBzYW5pdGl6ZVRleHQocmVzWzFdKS5zdHJpcCgpCiAg"
    "ICBmcmVlID0gMSBpZiBHREguZ3VpbGROYW1lRnJlZShuYW1lKSBlbHNlIDAKICAgIHByaW50KGYn"
    "W0xvYmJ5XSB7dXNyLnVzZXIubmFtZX0gY2hlY2tlZCBndWlsZCBuYW1lICJ7bmFtZX0iOiAnCiAg"
    "ICAgICAgICBmJ3siYXZhaWxhYmxlIiBpZiBmcmVlIGVsc2UgInJlamVjdGVkIn0nKQogICAgI0Vj"
    "aG8tcGx1cy1mbGFnLCB0aGUgc2FtZSBzaGFwZSB0aGUgY2xpZW50IGFscmVhZHkgYWNjZXB0cyBm"
    "cm9tCiAgICAjL3JlcXVlc3Rqb2luZ2FtZWNoYW5uZWwgKCIxIiBnbyBhaGVhZCAvICIwIiBubyku"
    "CiAgICByZXR1cm4gX2VtKGYnL3Rlc3RjcmVhdGVndWlsZCAie25hbWV9IiAie2ZyZWV9IicpCmRl"
    "ZiBfZ3VpbGRzbGFkZGVyKG1kLHVzcixyZXMpOgogICAgI1NlbnQgd2hlbiB0aGUgZ3VpbGQgc2Ny"
    "ZWVuIG9wZW5zLiBUaGUgbGF5b3V0IG9mIGFuIGluZGl2aWR1YWwgbGFkZGVyCiAgICAjZW50cnkg"
    "aXMgbm90IGtub3duLCBhbmQgdGhpcyBjbGllbnQgaXMgZnJhZ2lsZSBlbm91Z2ggdGhhdCBpbnZl"
    "bnRpbmcgb25lCiAgICAjcmlza3MgdGFraW5nIGl0IGRvd24gLSBzbyB0aGUgYW5zd2VyIGlzIGFu"
    "IGhvbmVzdCBlbXB0eSBsYWRkZXIsIHdoaWNoIGlzCiAgICAjYWxzbyB0aGUgdHJ1dGhmdWwgb25l"
    "IHVudGlsIGd1aWxkcyBjYW4gYWN0dWFsbHkgYmUgY3JlYXRlZC4gVGhlIGNvdW50CiAgICAjY29t"
    "ZXMgbGFzdCwgbWF0Y2hpbmcgL2pvaW5nYW1lY2hhbm5lbCdzIGVjaG8tcGx1cy1jb3VudCByZXBs"
    "eS4KICAgIHBhZ2UgPSBzYW5pdGl6ZVRleHQocmVzWzFdKSBpZiBsZW4ocmVzKSA+IDEgZWxzZSAn"
    "MScKICAgIHJldHVybiBfZW0oZicvZ3VpbGRzbGFkZGVyICJ7cGFnZX0iICIwIicpCmRlZiBfbGFk"
    "ZGVyKG1kLHVzcixyZXMpOgogICAgI1NlZW4gb25jZSBvbiB0aGUgd2lyZSwgcmlnaHQgYWZ0ZXIg"
    "YSBzdWNjZXNzZnVsIC9qb2luZ3VpbGQsIHdpdGggbm8KICAgICNhcmd1bWVudHMgY2FwdHVyZWQg"
    "LSBwcm9iYWJseSBhIHNlcnZlci13aWRlIGxlYWRlcmJvYXJkIHJhdGhlciB0aGFuIGEKICAgICNn"
    "dWlsZCBvbmUuIEl0cyByZXBseSBzaGFwZSBpcyBub3Qga25vd24uIEV2ZXJ5IG90aGVyIGNvbW1h"
    "bmQgaW4gdGhpcwogICAgI2ZpbGUgdGhhdCByZWFjaGVkIHRoaXMgc3RhdGUgd2FzIGFuc3dlcmVk"
    "IGJ5IG1hdGNoaW5nIGEgc2hhcGUgdGhlIGNsaWVudAogICAgI2hhZCBhbHJlYWR5IGJlZW4gc2Vl"
    "biBhY2NlcHRpbmcgZWxzZXdoZXJlIChlY2hvK2ZsYWcsIGVjaG8rY291bnQpOyB0aGVyZQogICAg"
    "I2lzIG5vIHN1Y2ggcHJlY2VkZW50IGZvciB0aGlzIG9uZS4gR3Vlc3NpbmcgYSBmaWVsZCBsYXlv"
    "dXQgcmlza3MgZmVlZGluZwogICAgI3RoaXMgY2xpZW50IGRhdGEgaXQgZG9lcyBub3QgZXhwZWN0"
    "LCBhbmQgaXQgaGFzIGFscmVhZHkgc2hvd24gaXRzZWxmCiAgICAjd2lsbGluZyB0byBjcmFzaCBv"
    "biBiYWQgaW5wdXQgcmF0aGVyIHRoYW4gcmVqZWN0IGl0IGdyYWNlZnVsbHkgLSBhIHdvcnNlCiAg"
    "ICAjb3V0Y29tZSB0aGFuIGEgVUkgZWxlbWVudCB0aGF0IHN0YXlzIGVtcHR5LiBSZWdpc3RlcmVk"
    "IHNvIGl0IHN0b3BzCiAgICAjc2hvd2luZyB1cCBhcyBhbiB1bmtub3duIGNvbW1hbmQ7IGRlbGli"
    "ZXJhdGVseSBhbnN3ZXJlZCB3aXRoIG5vdGhpbmcKICAgICN1bnRpbCBhIGNhcHR1cmUgc2hvd3Mg"
    "d2hhdCByZXBseSBpdCBhY3R1YWxseSB3YWl0cyBmb3IuCiAgICBwcmludChmJ1tMb2JieV0ge3Vz"
    "ci51c2VyLm5hbWV9IHNlbnQgL2xhZGRlciB7cmVzWzE6XSFyfSAtIG5vdCBhbnN3ZXJlZCwgJwog"
    "ICAgICAgICBmJ3NoYXBlIHVua25vd24gKHNlZSBjb21tZW50IGFib3ZlIF9sYWRkZXIpJykKICAg"
    "IHJldHVybiBOb25lCmRlZiBfam9pbmd1aWxkKG1kLHVzcixyZXMpOgogICAgI0NhcHR1cmVkIGZy"
    "b20gdGhlIHJldGFpbCBjbGllbnQ6IGFmdGVyIC90ZXN0Y3JlYXRlZ3VpbGQgYW5zd2VycyB0aGF0"
    "IGEKICAgICNuYW1lIGlzIGZyZWUsIHRoZSBjbGllbnQgY3JlYXRlcyB0aGUgZ3VpbGQgYnkgc2Vu"
    "ZGluZwogICAgIy9qb2luZ3VpbGQgIjxuYW1lPiIgIjEiICIxIi4gU28gdGhpcyBvbmUgY29tbWFu"
    "ZCBjb3ZlcnMgYm90aCBjcmVhdGluZyBhbmQKICAgICNqb2luaW5nLCBhbmQgd2hpY2ggaXQgaXMg"
    "Zm9sbG93cyBmcm9tIHdoZXRoZXIgdGhlIGd1aWxkIGFscmVhZHkgZXhpc3RzIC0KICAgICN0aGUg"
    "dHJhaWxpbmcgZmxhZ3MgYXJlIG5vdCBuZWVkZWQgdG8gdGVsbCB0aGVtIGFwYXJ0LiBBbnN3ZXJp"
    "bmcgbm90aGluZwogICAgI2hlcmUgaXMgd2hhdCBsZWZ0IHRoZSBndWlsZCBkaWFsb2cgc3Bpbm5p"
    "bmcuCiAgICBuYW1lID0gc2FuaXRpemVUZXh0KHJlc1sxXSkuc3RyaXAoKQogICAgaWYgR0RILmd1"
    "aWxkRXhpc3RzKG5hbWUpOgogICAgICAgIGVyciA9IEdESC5qb2luR3VpbGQobmFtZSwgdXNyLnVz"
    "ZXIubmFtZSkKICAgICAgICBhY3Rpb24gPSAnam9pbmVkJwogICAgZWxzZToKICAgICAgICBlcnIg"
    "PSBHREguY3JlYXRlR3VpbGQobmFtZSwgdXNyLnVzZXIubmFtZSkgI3ZhbGlkYXRlcyB0aGUgbmFt"
    "ZSBpdHNlbGYKICAgICAgICBhY3Rpb24gPSAnZm91bmRlZCcKICAgIGlmIGVycjoKICAgICAgICBy"
    "ZXR1cm4gX2VtKGYnL2Vycm9yIHtlcnJ9ICJ7bmFtZX0iJykKICAgICNDYW5vbmljYWwgc3BlbGxp"
    "bmcgZnJvbSB0aGUgZGF0YWJhc2UsIHdoaWNoIG1heSBkaWZmZXIgaW4gY2FzZSBmcm9tIHdoYXQK"
    "ICAgICN3YXMgdHlwZWQuCiAgICBuYW1lID0gR0RILmdldEd1aWxkTmFtZSh1c3IudXNlci5uYW1l"
    "KSBvciBuYW1lCiAgICB1c3IudXNlci5ndWlsZCA9IHNhbml0aXplVGV4dChuYW1lKQogICAgcHJp"
    "bnQoZidbTG9iYnldIHt1c3IudXNlci5uYW1lfSB7YWN0aW9ufSBndWlsZCAie25hbWV9IicpCiAg"
    "ICAjUmUtYW5ub3VuY2UgdGhlIHBsYXllciB0byB0aGVpciB0b3duIHNvIHRoZSBvdGhlcnMgcGlj"
    "ayB1cCB0aGUgbmV3IHRhZwogICAgI3dpdGhvdXQgcmVsb2dnaW5nLiBUaGlzIHJldXNlcyAkZ2Ft"
    "ZWNoYW5uZWx1c2VyIC0gYSBtZXNzYWdlIGZvcm1hdCB0aGUKICAgICNjbGllbnQgZGVtb25zdHJh"
    "Ymx5IGFjY2VwdHMgLSByYXRoZXIgdGhhbiBpbnZlbnRpbmcgYSBndWlsZC1zcGVjaWZpYyBvbmUu"
    "CiAgICBjaG5sID0gdXNyLnVzZXIuZ2FtZWNoYW5uZWwKICAgIGlmIGNobmw6CiAgICAgICAgbWQu"
    "YWRkKHsndGFyZ2V0Jzpfd29Vc2VyKGNobmwudXNlcmxpc3QsIHVzciksCiAgICAgICAgICAgICAg"
    "ICAnbWVzc2FnZSc6dXNyLnVzZXIuZ2V0R0NVbXNnKCl9KQogICAgI0VjaG8gcGx1cyBtZW1iZXIg"
    "Y291bnQsIHRoZSBzaGFwZSAvam9pbmdhbWVjaGFubmVsIGFscmVhZHkgcmVwbGllcyB3aXRoLgog"
    "ICAgcmV0dXJuIF9lbShmJy9qb2luZ3VpbGQgIntuYW1lfSIgIntsZW4oR0RILmdldEd1aWxkTWVt"
    "YmVycyhuYW1lKSl9IicpCiNUaGUgcm9vbSBuYW1lIGlzIHR5cGVkIGJ5IGEgcGxheWVyIGFuZCBp"
    "cyB0aGVuIGJyb2FkY2FzdCB0byBldmVyeW9uZSBicm93c2luZwojdGhlIHRvd24gaW5zaWRlIGEg"
    "cXVvdGVkICRnYW1lIGZpZWxkLiBJdCB3YXMgcGFzc2VkIHRocm91Z2ggdW50b3VjaGVkOiBhICci"
    "JyBpbgojaXQgZm9yZ2VkIHByb3RvY29sIGZpZWxkcyBmb3IgZXZlcnkgb3RoZXIgY2xpZW50LCBh"
    "bmQgaXRzIGxlbmd0aCB3YXMgdW5ib3VuZGVkLgojQm90aCBoYW5kbGVycyBtdXN0IGZvbGQgaXQg"
    "aWRlbnRpY2FsbHkgLSB0aGUgbmFtZSBpcyBhbHNvIHRoZSBkaWN0aW9uYXJ5IGtleQojdGhlIGNy"
    "ZWF0ZSByZXF1ZXN0IGlzIGxhdGVyIG1hdGNoZWQgYWdhaW5zdCwgc28gYW55IGRpZmZlcmVuY2Ug"
    "YmV0d2VlbiB0aGVtCiN3b3VsZCB0dXJuIGEgbGVnaXRpbWF0ZSBjcmVhdGlvbiBpbnRvICJnYW1l"
    "TmFtZVRha2VuIi4KZGVmIF9nYW1lTmFtZShyYXcpOgogICAgcmV0dXJuIHNhbml0aXplVGV4dChy"
    "YXcsIF9NQVhfR0FNRU5BTUUpCmRlZiBfcmVxdWVzdGNyZWF0ZWdhbWUobWQsdXNyLHJlcyk6CiAg"
    "ICBpZiBub3QgdXNyLnVzZXIuZ2FtZWNoYW5uZWw6CiAgICAgICAgcmV0dXJuIE5vbmUgI25vdCBp"
    "biBhIGdhbWUgY2hhbm5lbCAtIHVzZWQgdG8gcmFpc2UgQXR0cmlidXRlRXJyb3Igb24KICAgICAg"
    "ICAgICAgICAgICAgICAjTm9uZSBhbmQga2lsbCB0aGUgY29ubmVjdGlvbidzIGhhbmRsZXIgdGhy"
    "ZWFkCiAgICByZXR1cm4gdXNyLnVzZXIuZ2FtZWNoYW5uZWwucmVxdWVzdENyZWF0ZUdhbWUodXNy"
    "LCBfZ2FtZU5hbWUocmVzWzFdKSkKZGVmIF9jcmVhdGVHYW1lKG1kLHVzcixyZXMpOgogICAgaWYg"
    "bm90IHVzci51c2VyLmdhbWVjaGFubmVsOgogICAgICAgIHJldHVybiBOb25lICNzZWUgX3JlcXVl"
    "c3RjcmVhdGVnYW1lCiAgICByZXR1cm4gdXNyLnVzZXIuZ2FtZWNoYW5uZWwuY3JlYXRlR2FtZShf"
    "Z2FtZU5hbWUocmVzWzFdKSwgdXNyLCByZXNbMl0sIHJlc1szXSwgcmVzWzRdLCByZXNbNV0sIHJl"
    "c1s2XSwgcmVzWzddLCByZXNbOF0sIHJlc1s5XSkKZGVmIF9zdG9wZ2FtZShtZCx1c3IscmVzKToK"
    "ICAgIGlmIHVzci51c2VyLmdhbWU6CiAgICAgICAgcmV0dXJuIHVzci51c2VyLmdhbWUucmVtb3Zl"
    "KHVzcikKICAgICNwcmludCgnVXNlciBpcyBub3QgaW4gYSBnYW1lJykKICAgIHJldHVybiBOb25l"
    "CmRlZiBfc3RhcnRpbmdnYW1lKG1kLHVzcixyZXMpOgogICAgaWYgdXNyLnVzZXIuZ2FtZToKICAg"
    "ICAgICByZXR1cm4gdXNyLnVzZXIuZ2FtZS5zdGFydEdhbWUodXNyKQogICAgcmV0dXJuIE5vbmUg"
    "I1RPRE8gd2hhdCBkb2VzIHRoaXMgZXZlbiBkbz8KZGVmIF9zdGFydGdhbWUobWQsdXNyLHJlcyk6"
    "CiAgICAjVE9ETyBoYW5kbGUgcHJvcGVybHkKICAgIGlmIHVzci51c2VyLmdhbWU6CiAgICAgICAg"
    "cGFzcwogICAgcmV0dXJuIE5vbmUKZGVmIF9nYW1lY29tbWFuZHRvdXNlcihtZCx1c3IscmVzKToK"
    "ICAgIGRhdCA9IF9SZWFkQmxvYih1c3IsIHJlc1syXSkKICAgIHRjb24gPSB1c3Iuc2VydmVyLmdl"
    "dFBsYXllcihyZXNbMV0pCiAgICAjQWxsb3cgY29tbWFuZHMgdG8gYW55IGNvbm5lY3RlZCBwbGF5"
    "ZXIsIHJlZ2FyZGxlc3Mgb2Ygc3RhdGUsIHRvIHN1cHBvcnQgbW9kZGVkIHVzZXMKICAgIGlmIG5v"
    "dCB0Y29uOgogICAgICAgICNwcmludCgnUGxheWVyOicscmVzWzFdLCdkb2VzIG5vdCBleGlzdD8n"
    "KQogICAgICAgIHJldHVybiBOb25lCiAgICAjVE9ETyBjb25zaWRlciBvcHRpbWlzaW5nIHRoaXMg"
    "Y29tbWFuZCBpbiBwYXJ0aWN1bGFyCiAgICBmdWxtc2cgPSBfZW0oZicvZ2FtZWNvbW1hbmR0b3Vz"
    "ZXIgInt1c3IudXNlci5uYW1lfSIgIntsZW4oZGF0KX0iJykrZGF0CiAgICAjU3RyYWlnaHQgb250"
    "byB0aGUgcmVjaXBpZW50J3Mgb3duIG91dGJvdW5kIHF1ZXVlIGluc3RlYWQgb2YgdmlhIHRoZQog"
    "ICAgI3NlcnZlci13aWRlIE1lc3NhZ2VEaXN0cmlidXRvci4gVGhpcyBpcyB0aGUgY29tbWFuZCB0"
    "aGF0IGNhcnJpZXMgdGhlCiAgICAjYWN0dWFsIGluLWdhbWUgdHJhZmZpYyBiZXR3ZWVuIHR3byBw"
    "bGF5ZXJzLCBpdCBhbHdheXMgaGFzIGV4YWN0bHkgb25lCiAgICAjcmVjaXBpZW50LCBhbmQgc2Vu"
    "ZCgpIGlzIGp1c3QgYSBxdWV1ZSBwdXQgLSBzbyB0aGUgZGlzdHJpYnV0b3IgaG9wIGJvdWdodAog"
    "ICAgI25vdGhpbmcgYnV0IGxhdGVuY3kuIFdvcnNlLCB0aGF0IHNpbmdsZSBkaXN0cmlidXRvciB0"
    "aHJlYWQgaXMgc2hhcmVkIGJ5CiAgICAjZXZlcnkgY29ubmVjdGlvbiBvbiB0aGUgc2VydmVyOiBv"
    "bmUgc2xvdyBmYW4tb3V0IChhIHBvc2l0aW9uIGJyb2FkY2FzdCB0bwogICAgI2EgZnVsbCB0b3du"
    "LCBhIGhlcm9kYXRhIGJsb2IpIHF1ZXVlZCBhaGVhZCBvZiBhIGdhbWUgY29tbWFuZCBkZWxheWVk"
    "IGl0CiAgICAjZm9yIGV2ZXJ5b25lLiBEaXJlY3QgaGFuZC1vZmYgcmVtb3ZlcyBib3RoIHRoZSBl"
    "eHRyYSB0aHJlYWQgd2FrZS11cCBhbmQKICAgICN0aGF0IGhlYWQtb2YtbGluZSBibG9ja2luZywg"
    "YW5kIHJlbGF5IG9yZGVyIGJldHdlZW4gYW55IGdpdmVuIHBhaXIgb2YKICAgICNwbGF5ZXJzIGlz"
    "IHN0aWxsIHByZXNlcnZlZCBiZWNhdXNlIHRoZXkgYWxsIHRha2UgdGhpcyBzYW1lIHBhdGguCiAg"
    "ICB0Y29uLnNlbmQoZnVsbXNnKQogICAgcmV0dXJuIE5vbmUKZGVmIF9qb2luZ2FtZShtZCx1c3Is"
    "cmVzKToKICAgIGlmIG5vdCB1c3IudXNlci5nYW1lY2hhbm5lbDoKICAgICAgICByZXR1cm4gX2Vt"
    "KGYnL2Vycm9yIHVua25vd25HYW1lICJ7cmVzWzFdfSInKSAjbm90IGluIGEgZ2FtZSBjaGFubmVs"
    "CiAgICBnbSA9IHVzci51c2VyLmdhbWVjaGFubmVsLmdhbWVzLmdldChfZ2FtZU5hbWUocmVzWzFd"
    "KSxOb25lKQogICAgaWYgZ20gPT0gTm9uZToKICAgICAgICAjQW5zd2VyLCBkb24ndCBpZ25vcmU6"
    "IHRoZSBjbGllbnQgaXMgc2l0dGluZyBvbiBhICJjb25uZWN0aW5nIiBkaWFsb2cKICAgICAgICAj"
    "dGhhdCBvbmx5IGEgcmVwbHkgZGlzbWlzc2VzLiBIYXBwZW5zIHdoZW5ldmVyIHRoZSByb29tIGlz"
    "IHRvcm4gZG93bgogICAgICAgICNiZXR3ZWVuIHRoZSBwbGF5ZXIgc2VlaW5nIGl0IGluIHRoZSBs"
    "aXN0IGFuZCBjbGlja2luZyBpdC4KICAgICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIHVua25vd25H"
    "YW1lICJ7cmVzWzFdfSInKQogICAgI1RoZSBwYXNzd29yZCBhcmd1bWVudCBpcyBhYnNlbnQgd2hl"
    "biB0aGUgcm9vbSBoYXMgbm9uZSAtIHNlZSB0aGUgYXJpdHkKICAgICNub3RlIG9uIF9DT01NQU5E"
    "Uy4KICAgIHJldHVybiBnbS5hZGRVc2VyKHVzciwgcmVzWzJdIGlmIGxlbihyZXMpPjIgZWxzZSAn"
    "JykKZGVmIF93aG9pcyhtZCx1c3IscmVzKToKICAgIGlmIGxlbihyZXMpPDI6CiAgICAgICAgcmV0"
    "dXJuIE5vbmUKICAgIHRhcmdldCA9IHJlc1sxXQogICAgaW5mbyA9IEdESC5nZXRXaG9pcyh0YXJn"
    "ZXQpCiAgICBpZiBpbmZvIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIE5vbmUgI3Vua25vd24gdXNl"
    "cgogICAgdGNvbiA9IHVzci5zZXJ2ZXIuZ2V0UGxheWVyKHRhcmdldCkKICAgIHRvd24gPSB0Y29u"
    "LnVzZXIuZ2FtZWNoYW5uZWwubmFtZSBpZiAodGNvbiBhbmQgdGNvbi51c2VyLmdhbWVjaGFubmVs"
    "KSBlbHNlICcnCiAgICBjaGF0Y2hhbm5lbCA9ICcnCiAgICBpZiB0Y29uIGFuZCB0Y29uLnVzZXIu"
    "Y2hhdGNoYW5uZWw6CiAgICAgICAgI1RoZSB0YXJnZXQncyBjaGF0IGNoYW5uZWwgaXMgYSBwbGFp"
    "biBsaXN0LCBzbyBpdCBpcyBpZGVudGlmaWVkIGJ5CiAgICAgICAgI3NlYXJjaGluZyBmb3IgdGhl"
    "IG9iamVjdC4gU3RvcCBhdCB0aGUgZmlyc3QgbWF0Y2ggaW5zdGVhZCBvZiB3YWxraW5nCiAgICAg"
    "ICAgI2V2ZXJ5IGNoYW5uZWwgb2YgZXZlcnkgdG93biBhZnRlcndhcmRzIC0gYW5kIHRha2UgdGhl"
    "IG5hbWUgZnJvbSB0aGUKICAgICAgICAjdG93biB0aGUgcGxheWVyIGlzIGFjdHVhbGx5IGluLCB3"
    "aGljaCB0aGUgdW5icm9rZW4gbG9vcCBjb3VsZCBvdmVyd3JpdGUKICAgICAgICAjd2l0aCBhIGxh"
    "dGVyIHRvd24ncyBpZGVudGljYWxseS1uYW1lZCBjaGFubmVsLgogICAgICAgIGZvciBjaG4gaW4g"
    "bGlzdCh1c3Iuc2VydmVyLnN0YXRlLmdhbWVDaGFubmVscy52YWx1ZXMoKSk6CiAgICAgICAgICAg"
    "IGZvciBjbmFtZSwgdWxpc3QgaW4gbGlzdChjaG4uY2hhdENoYW5uZWxzLml0ZW1zKCkpOgogICAg"
    "ICAgICAgICAgICAgaWYgdWxpc3QgaXMgdGNvbi51c2VyLmNoYXRjaGFubmVsOgogICAgICAgICAg"
    "ICAgICAgICAgIGNoYXRjaGFubmVsID0gY25hbWUKICAgICAgICAgICAgICAgICAgICBicmVhawog"
    "ICAgICAgICAgICBpZiBjaGF0Y2hhbm5lbDoKICAgICAgICAgICAgICAgIGJyZWFrCiAgICBndWls"
    "ZCA9IHNhbml0aXplVGV4dChHREguZ2V0R3VpbGROYW1lKHRhcmdldCkpCiAgICAjQ2FwcGVkIGFn"
    "YWluIG9uIHRoZSB3YXkgb3V0LCBub3Qgb25seSBvbiB0aGUgd2F5IGluOiByb3dzIHdyaXR0ZW4g"
    "YmVmb3JlCiAgICAjL3VwZGF0ZSB3YXMgYm91bmRlZCBhcmUgc3RpbGwgaW4gdGhlIGRhdGFiYXNl"
    "LCBhbmQgdGhpcyBpcyB0aGUgbWVzc2FnZSB0aGF0CiAgICAjaGFuZHMgdGhlbSB0byBhICpkaWZm"
    "ZXJlbnQqIHBsYXllcidzIGNsaWVudC4KICAgIHJldHVybiBfZW0oCiAgICAgICAgZicvd2hvaXMg"
    "Int0YXJnZXR9IiAie2d1aWxkfSIgIntzYW5pdGl6ZVRleHQodG93bil9IiAie3Nhbml0aXplVGV4"
    "dChjaGF0Y2hhbm5lbCl9IiAnCiAgICAgICAgZicie3Nhbml0aXplVGV4dChpbmZvWyJlbWFpbCJd"
    "LCBfTUFYX1dIT0lTX0ZJRUxEKX0iICcKICAgICAgICBmJyJ7c2FuaXRpemVUZXh0KGluZm9bImxv"
    "Y2F0aW9uIl0sIF9NQVhfV0hPSVNfRklFTEQpfSIgJwogICAgICAgIGYne2luZm9bImFnZSJdfSB7"
    "aW5mb1siZ2VuZGVyIl19ICJ7c2FuaXRpemVUZXh0KGluZm9bImRlc2NyaXB0aW9uIl0sIF9NQVhf"
    "REVTQ1JJUFRJT04pfSInCiAgICApCmRlZiBfdXBkYXRlKG1kLHVzcixyZXMpOgogICAgIy91cGRh"
    "dGUgIm5hbWUiICJlbWFpbCIgImxvY2F0aW9uIiAiYWdlIiAiZ2VuZGVyIiAiZGVzY3JpcHRpb24i"
    "CiAgICBpZiBsZW4ocmVzKTw2OgogICAgICAgIHJldHVybiBOb25lCiAgICBpZiByZXNbMV0gIT0g"
    "dXNyLnVzZXIubmFtZToKICAgICAgICByZXR1cm4gTm9uZSAjY2FuIG9ubHkgdXBkYXRlIG93biB3"
    "aG9pcyBpbmZvCiAgICBlbWFpbCA9IHNhbml0aXplVGV4dChyZXNbMl0sIF9NQVhfV0hPSVNfRklF"
    "TEQpCiAgICBsb2NhdGlvbiA9IHNhbml0aXplVGV4dChyZXNbM10sIF9NQVhfV0hPSVNfRklFTEQp"
    "CiAgICBhZ2UgPSByZXNbNF0KICAgIGdlbmRlciA9IHJlc1s1XQogICAgZGVzY3JpcHRpb24gPSBz"
    "YW5pdGl6ZVRleHQocmVzWzZdLCBfTUFYX0RFU0NSSVBUSU9OKSBpZiBsZW4ocmVzKT42IGVsc2Ug"
    "JycKICAgIEdESC51cGRhdGVXaG9pcyh1c3IudXNlci5uYW1lLCBlbWFpbCwgbG9jYXRpb24sIGFn"
    "ZSwgZ2VuZGVyLCBkZXNjcmlwdGlvbikKICAgIHJldHVybiBOb25lICNzZXJ2ZXIgc2VuZHMgbm8g"
    "cmVzcG9uc2UsIHBlciBwcm90b2NvbCBkb2MKCl9SRV9DTUQgPSByZS5jb21waWxlKHInKD86Iihb"
    "XiJdKikiKXwoW15cc10rKScpCiNjb21tYW5kIC0+IChoYW5kbGVyLCBtaW5pbXVtIGFyZ3VtZW50"
    "IGNvdW50ICpleGNsdWRpbmcqIHRoZSBjb21tYW5kIHdvcmQpLgojVGhlIGNvdW50IGlzIGVuZm9y"
    "Y2VkIG9uY2UsIGNlbnRyYWxseSwgaW4gcGFyc2UoKTogZXZlcnkgaGFuZGxlciBpbmRleGVzIGlu"
    "dG8KI3Jlc1tdIHBvc2l0aW9uYWxseSwgc28gYSBjbGllbnQgc2VuZGluZyBhIGNvbW1hbmQgd2l0"
    "aCBmZXdlciBhcmd1bWVudHMgdGhhbgojZXhwZWN0ZWQgdXNlZCB0byByYWlzZSBJbmRleEVycm9y"
    "IGFuZCB0ZWFyIGRvd24gaXRzIG93biBjb25uZWN0aW9uIHRocmVhZC4KI0RlY2xhcmluZyB0aGUg"
    "YXJpdHkgaGVyZSBrZWVwcyB0aGF0IGNoZWNrIGluIG9uZSBwbGFjZSBpbnN0ZWFkIG9mIHJlcGVh"
    "dGluZyBhCiNsZW4ocmVzKSBndWFyZCBhdCB0aGUgdG9wIG9mIGZpZnRlZW4gaGFuZGxlcnMuCl9D"
    "T01NQU5EUyA9IHsKICAgICcvbm9wJzogICAgICAgICAgICAgICAgICAgIChfbm9wLCAwKSwKICAg"
    "ICcvbGVhdmVnYW1lY2hhbm5lbCc6ICAgICAgIChfbGVhdmVnYW1lY2hhbm5lbCwgMCksCiAgICAn"
    "L3JlcXVlc3Rqb2luZ2FtZWNoYW5uZWwnOiAoX3JlcXVlc3Rqb2luZ2FtZWNoYW5uZWwsIDEpLAog"
    "ICAgI0FyaXR5IDEsIG5vdCAyOiB0aGUgcG9zaXRpb24gYXJndW1lbnQgaXMgb3B0aW9uYWwgKHRo"
    "ZSBjbGllbnQgb21pdHMgaXQKICAgICN3aGVuIGl0IGhhcyBubyBsYXN0LWtub3duIHBvc2l0aW9u"
    "IHlldCwgZS5nLiB0aGUgdmVyeSBmaXJzdCB0b3duIGVudHJ5CiAgICAjYWZ0ZXIgbG9naW4pLiBS"
    "ZXF1aXJpbmcgaXQgbWFkZSBwYXJzZSgpIGRyb3AgdGhlIGNvbW1hbmQgc2lsZW50bHksIHdoaWNo"
    "CiAgICAjdGhlIGNsaWVudCBleHBlcmllbmNlcyBhcyBhIHRvd24gaXQgY2FuIG5ldmVyIGZpbmlz"
    "aCBsb2FkaW5nLgogICAgJy9qb2luZ2FtZWNoYW5uZWwnOiAgICAgICAgKF9qb2luZ2FtZWNoYW5u"
    "ZWwsIDEpLAogICAgJy91cGRoZXJvcG9zJzogICAgICAgICAgICAgKF91cGRoZXJvcG9zLCAxKSwK"
    "ICAgICcvc2VuZCc6ICAgICAgICAgICAgICAgICAgIChfc2VuZCwgMSksCiAgICAnL2dldGd1aWxk"
    "cmFua3BvaW50cyc6ICAgICAoX2dldGd1aWxkcmFua3BvaW50cywgMCksCiAgICAnL3JlcXVlc3Rj"
    "cmVhdGVnYW1lJzogICAgICAoX3JlcXVlc3RjcmVhdGVnYW1lLCAxKSwKICAgICcvY3JlYXRlZ2Ft"
    "ZSc6ICAgICAgICAgICAgIChfY3JlYXRlR2FtZSwgOSksCiAgICAnL3N0b3BnYW1lJzogICAgICAg"
    "ICAgICAgICAoX3N0b3BnYW1lLCAwKSwKICAgICcvbGVhdmVnYW1lJzogICAgICAgICAgICAgIChf"
    "c3RvcGdhbWUsIDApLCNUT0RPIGZpeCBmb3IgbXVsdGlwbGUgdXNlcnM/CiAgICAnL3N0YXJ0aW5n"
    "Z2FtZSc6ICAgICAgICAgICAoX3N0YXJ0aW5nZ2FtZSwgMCksCiAgICAnL3N0YXJ0Z2FtZSc6ICAg"
    "ICAgICAgICAgICAoX3N0YXJ0Z2FtZSwgMCksCiAgICAnL2dldHBsYXllcmRhdGEnOiAgICAgICAg"
    "ICAoX2dldHBsYXllcmRhdGEsIDIpLAogICAgJy9zZXRwbGF5ZXJkYXRhJzogICAgICAgICAgKF9z"
    "ZXRwbGF5ZXJkYXRhLCAzKSwKICAgICcvc2V0dXNlcmhlcm9kYXRhJzogICAgICAgIChfc2V0dXNl"
    "cmhlcm9kYXRhLCAyKSwKICAgICcvZ2FtZWNvbW1hbmR0b3VzZXInOiAgICAgIChfZ2FtZWNvbW1h"
    "bmR0b3VzZXIsIDIpLCNUT0RPIGNvbnNpZGVyIG9wdGltaXNpbmcKICAgICNBcml0eSAxOiB0aGUg"
    "cGFzc3dvcmQgYXJndW1lbnQgaXMgYWJzZW50IGZvciBhIHJvb20gdGhhdCBoYXMgbm9uZSwgYW5k"
    "CiAgICAjZHJvcHBpbmcgdGhlIGNvbW1hbmQgbGVmdCB0aGUgam9pbmluZyBwbGF5ZXIgb24gImNv"
    "bm5lY3RpbmciIGZvcmV2ZXIuCiAgICAnL2pvaW5nYW1lJzogICAgICAgICAgICAgICAoX2pvaW5n"
    "YW1lLCAxKSwKICAgICcvd2hvaXMnOiAgICAgICAgICAgICAgICAgIChfd2hvaXMsIDEpLAogICAg"
    "Jy91cGRhdGUnOiAgICAgICAgICAgICAgICAgKF91cGRhdGUsIDUpLAogICAgI0FyaXRpZXMgYmVs"
    "b3cgYXJlIHRoZSBjbGllbnQncyBvd24sIGZyb20gaXRzIGZvcm1hdCB0YWJsZSAtIHNlZSB0aGUg"
    "YmxvY2sKICAgICNvZiBoYW5kbGVycyBhYm92ZS4gL21zZydzIGxheW91dCBpcyBub3QgaW4gdGhh"
    "dCB0YWJsZSAodGhlIGNsaWVudCBidWlsZHMgaXQKICAgICNieSBjb25jYXRlbmF0aW9uLCBsaWtl"
    "IC9zZW5kKSwgc28gMiBpcyB0aGUgc21hbGxlc3Qgc2FuZSByZXF1aXJlbWVudC4KICAgICcvZ2Ft"
    "ZWNoYW5uZWxzbGlzdCc6ICAgICAgIChfZ2FtZWNoYW5uZWxzbGlzdCwgMCksCiAgICAnL2pvaW5j"
    "aGF0Y2hhbm5lbCc6ICAgICAgICAoX2pvaW5jaGF0Y2hhbm5lbCwgMSksCiAgICAnL21zZyc6ICAg"
    "ICAgICAgICAgICAgICAgICAoX21zZywgMiksCiAgICAnL3NldGdhbWVwYXJhbXMnOiAgICAgICAg"
    "ICAoX3NldGdhbWVwYXJhbXMsIDIpLAogICAgJy9uZXdnYW1laG9zdCc6ICAgICAgICAgICAgKF9u"
    "ZXdnYW1laG9zdCwgMSksCiAgICAjR3VpbGRzLiBFdmVyeSBuYW1lIGhlcmUgaGFzIGJlZW4gc2Vl"
    "biBvbiB0aGUgd2lyZSBmcm9tIHRoZSByZXRhaWwgY2xpZW50LgogICAgI1RoZSBiYXRjaCBvZiBn"
    "dWVzc2VkIHNwZWxsaW5ncyB0aGF0IHVzZWQgdG8gc2l0IGFsb25nc2lkZSB0aGVtCiAgICAjKC9j"
    "cmVhdGVndWlsZCwgL3JlcXVlc3RjcmVhdGVndWlsZCwgL2NyZWF0Z3VpbGQsIC9ndWlsZGNyZWF0"
    "ZSwKICAgICMvcmVxdWVzdGpvaW5ndWlsZCwgL3F1aXRndWlsZCwgL2dldGd1aWxkaW5mbykgaXMg"
    "Z29uZTogdGhlIGNhcHR1cmUgc2hvd2VkCiAgICAjdGhlIGNsaWVudCBzZW5kcyBub25lIG9mIHRo"
    "ZW0sIGFuZCB0aGF0IC9qb2luZ3VpbGQgaXMgd2hhdCBjcmVhdGVzIGEKICAgICNndWlsZC4gTGVh"
    "dmluZyBhIGd1aWxkIGhhcyBub3QgYmVlbiBvYnNlcnZlZCB5ZXQsIHNvIG5vIGhhbmRsZXIgaXMK"
    "ICAgICNyZWdpc3RlcmVkIGZvciBpdCAtIHRoZSByZWFsIG5hbWUgd2lsbCBzaG93IHVwIGluIHRo"
    "ZSBsb2cgYXMgYW4gdW5rbm93bgogICAgI2NvbW1hbmQgdGhlIGZpcnN0IHRpbWUgc29tZWJvZHkg"
    "dHJpZXMuCiAgICAnL2d1aWxkc2xhZGRlcic6ICAgICAgICAgICAoX2d1aWxkc2xhZGRlciwgMSks"
    "CiAgICAnL3Rlc3RjcmVhdGVndWlsZCc6ICAgICAgICAoX3Rlc3RjcmVhdGVndWlsZCwgMSksCiAg"
    "ICAnL2pvaW5ndWlsZCc6ICAgICAgICAgICAgICAoX2pvaW5ndWlsZCwgMSksCiAgICAnL2xhZGRl"
    "cic6ICAgICAgICAgICAgICAgICAoX2xhZGRlciwgMCksCn0KY2xhc3MgQ29tbWFuZFBhcnNlcigp"
    "OgogICAgZGVmIF9faW5pdF9fKHNlbGYsIG1zZ2VyKToKICAgICAgICBzZWxmLmNvbW1hbmRsaXN0"
    "ID0gX0NPTU1BTkRTCiAgICAgICAgc2VsZi5tZCA9IG1zZ2VyCgogICAgZGVmIHBhcnNlKHNlbGYs"
    "IGRhdGEsIG9yaWdpbik6CiAgICAgICAgI3ByaW50KGYnVGVzdCBQYXJzaW5nIHtsZW4oZGF0YSl9"
    "OiB7Ynl0ZXMoZGF0YSwgJ2FzY2lpJyl9JykKICAgICAgICByZXMgPSBsaXN0KCAoaXRtWzBdK2l0"
    "bVsxXSBmb3IgaXRtIGluIF9SRV9DTUQuZmluZGFsbChkYXRhKSkgKQogICAgICAgICNwcmludCgn"
    "UmVzOicsIHJlcykKICAgICAgICBpZiBub3QgcmVzOgogICAgICAgICAgICAjV2FzIGEgc2lsZW50"
    "IGRyb3AuIElmIGEgZmVhdHVyZSBkb2VzIG5vdGhpbmcgYW5kIHRoZSBsb2cgc2hvd3Mgbm8KICAg"
    "ICAgICAgICAgI2NvbW1hbmQgZm9yIGl0IGF0IGFsbCwgdGhpcyBpcyBvbmUgb2YgdGhlIHR3byBw"
    "bGFjZXMgaXQgY291bGQKICAgICAgICAgICAgI2hhdmUgZGlzYXBwZWFyZWQgaW50byAtIHNvIHNh"
    "eSBzbyByYXRoZXIgdGhhbiBsZWF2ZSBhIGJsaW5kIHNwb3QuCiAgICAgICAgICAgIGlmIF9ERUJV"
    "R19MT0dfQ09NTUFORFMgYW5kIGRhdGE6CiAgICAgICAgICAgICAgICB3aG8gPSBvcmlnaW4udXNl"
    "ci5uYW1lIGlmIG9yaWdpbi51c2VyIGVsc2UgJz8nCiAgICAgICAgICAgICAgICBwcmludChmJ1tj"
    "bWRdIHt3aG99IC0+IChVTlBBUlNFQUJMRSkge2RhdGEhcn0nKQogICAgICAgICAgICByZXR1cm4g"
    "Tm9uZQogICAgICAgIHdobyA9IG9yaWdpbi51c2VyLm5hbWUgaWYgb3JpZ2luLnVzZXIgZWxzZSAn"
    "PycKICAgICAgICBsb3VkID0gX0RFQlVHX0xPR19DT01NQU5EUyBhbmQgKF9ERUJVR19MT0dfVkVS"
    "Qk9TRSBvciByZXNbMF0gbm90IGluIF9RVUlFVF9DT01NQU5EUykKICAgICAgICBpZiBsb3VkOgog"
    "ICAgICAgICAgICBwcmludChmJ1tjbWRdIHt3aG99IC0+IHtkYXRhfScpCiAgICAgICAgZW50cnkg"
    "PSBzZWxmLmNvbW1hbmRsaXN0LmdldChyZXNbMF0pCiAgICAgICAgaWYgZW50cnkgaXMgTm9uZToK"
    "ICAgICAgICAgICAgI0xvZyB0aGUgcmF3IGxpbmUsIG5vdCBqdXN0IHRoZSB0b2tlbmlzZWQgbGlz"
    "dC4gQW4gdW5pbXBsZW1lbnRlZAogICAgICAgICAgICAjY29tbWFuZCBpcyBleGFjdGx5IHRoZSBz"
    "aXR1YXRpb24gd2hlcmUgdGhlIGFyZ3VtZW50IGxheW91dCBpcwogICAgICAgICAgICAjd2hhdCB3"
    "ZSBuZWVkIHRvIHNlZSwgYW5kIHJlLXF1b3RpbmcgdGhlIHNwbGl0IHRva2VucyBsb3NlcyBpdC4K"
    "ICAgICAgICAgICAgcHJpbnQoZicqKiogVU5LTk9XTiBDT01NQU5EIGZyb20ge3dob306IHtkYXRh"
    "IXJ9JykKICAgICAgICAgICAgcmV0dXJuIE5vbmUKICAgICAgICBoYW5kbGVyLCBtaW5hcmdzID0g"
    "ZW50cnkKICAgICAgICBpZiBsZW4ocmVzKSAtIDEgPCBtaW5hcmdzOgogICAgICAgICAgICBwcmlu"
    "dChmJyoqKiBNQUxGT1JNRUQgQ09NTUFORCBmcm9tIHt3aG99OiAnCiAgICAgICAgICAgICAgICAg"
    "IGYne3Jlc1swXX0gbmVlZHMge21pbmFyZ3N9IGFyZ3VtZW50KHMpLCBnb3Qge2xlbihyZXMpLTF9"
    "JykKICAgICAgICAgICAgcmV0dXJuIE5vbmUKICAgICAgICAjcHJpbnQoZidQYXJzZWQgQ29tbWFu"
    "ZCBGcm9tIHtvcmlnaW4udXNlci5uYW1lfTonLCByZXMpCiAgICAgICAgb3V0ID0gaGFuZGxlcihz"
    "ZWxmLm1kLCBvcmlnaW4sIHJlcykKICAgICAgICBpZiBsb3VkOgogICAgICAgICAgICAjIihubyBk"
    "aXJlY3QgcmVwbHkpIiBpcyB0aGUgc2lnbmF0dXJlIG9mIGV2ZXJ5IGhhbmcgcmVwb3J0ZWQgc28K"
    "ICAgICAgICAgICAgI2ZhcjogdGhlIGNsaWVudCB3YWl0cyBvbiBhbiBhbnN3ZXIgdGhhdCB0aGlz"
    "IHNlcnZlciBuZXZlciBzZW5kcy4KICAgICAgICAgICAgI1NvbWUgY29tbWFuZHMgbGVnaXRpbWF0"
    "ZWx5IGFuc3dlciB3aXRoIG5vdGhpbmcsIHNvIHRoaXMgaXMgYSBsZWFkLAogICAgICAgICAgICAj"
    "bm90IGEgdmVyZGljdCAtIGJ1dCBpdCBpcyB0aGUgZmlyc3QgdGhpbmcgdG8gbG9vayBhdC4KICAg"
    "ICAgICAgICAgaWYgb3V0OgogICAgICAgICAgICAgICAgaGVhZCA9IG91dC5zcGxpdChfTilbMF0u"
    "ZGVjb2RlKF9XSVJFX0VOQywgJ3JlcGxhY2UnKQogICAgICAgICAgICAgICAgcHJpbnQoZidbY21k"
    "XSB7d2hvfSA8LSB7aGVhZH0nKQogICAgICAgICAgICBlbHNlOgogICAgICAgICAgICAgICAgcHJp"
    "bnQoZidbY21kXSB7d2hvfSA8LSAobm8gZGlyZWN0IHJlcGx5KScpCiAgICAgICAgcmV0dXJuIG91"
    "dAoKI3RocmVhZCB0byBzZW5kIG1lc3NhZ2VzIGFjcm9zcyBhbGwgY29ubmVjdGVkIGNsaWVudHMK"
    "I19fRVhBTVBMRV9NRVNTQUdFX18gPSB7CiMgICAgJ3RhcmdldCc6Wyd1c2VybGlzdCddLAojICAg"
    "ICdtZXNzYWdlJzpiJy93aGF0ZXZlclwwJytiJ2Jsb2InCiN9CmNsYXNzIE1lc3NhZ2VEaXN0cmli"
    "dXRvcigpOgogICAgX0VORElURU0gPSBbJ1NUT1AnXQogICAgZGVmIF9faW5pdF9fKHNlbGYsIHNl"
    "cnZlcik6CiAgICAgICAgc2VsZi5fY1F1ZXVlID0gU2ltcGxlUXVldWUoKQogICAgICAgIHNlbGYu"
    "c2VydmVyID0gc2VydmVyCiAgICBkZWYgc2VydmVfZm9yZXZlcihzZWxmKToKICAgICAgICB3aGls"
    "ZSBUcnVlOiAjVE9ETyBwb3NzaWJsZSBjaGVjayBzZWxmLnNlcnZlci5faXNfY2xvc2luZwogICAg"
    "ICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICBjb21tYW5kID0gc2VsZi5fY1F1ZXVlLmdldCgp"
    "CiAgICAgICAgICAgICAgICAjcHJpbnQoJ01EOicsIGNvbW1hbmQsIHNlbGYuc2VydmVyLl9pc19j"
    "bG9zaW5nKQogICAgICAgICAgICAgICAgaWYgY29tbWFuZCA9PSBzZWxmLl9FTkRJVEVNOgogICAg"
    "ICAgICAgICAgICAgICAgIGJyZWFrCiAgICAgICAgICAgICAgICB1bCA9IGNvbW1hbmQuZ2V0KCd0"
    "YXJnZXQnLFtdKQogICAgICAgICAgICAgICAgbXNnID0gY29tbWFuZC5nZXQoJ21lc3NhZ2UnKQog"
    "ICAgICAgICAgICAgICAgaWYgbXNnOgogICAgICAgICAgICAgICAgICAgIGZvciB1c3IgaW4gdWw6"
    "CiAgICAgICAgICAgICAgICAgICAgICAgIHVzci5zZW5kKG1zZykKICAgICAgICAgICAgZXhjZXB0"
    "IEV4Y2VwdGlvbjoKICAgICAgICAgICAgICAgIHByaW50KCdbTG9iYnldIERpc3RyaWJ1dG9yIGVy"
    "cm9yOlxuJyArIHRyYWNlYmFjay5mb3JtYXRfZXhjKCkpCiAgICBkZWYgYWRkKHNlbGYsIHByb3Bz"
    "KToKICAgICAgICAjU25hcHNob3QgdGhlIHRhcmdldCBsaXN0IEhFUkUsIGluIHRoZSBjYWxsaW5n"
    "IHRocmVhZC4gQ2FsbGVycyBoYW5kIHVzCiAgICAgICAgI2xpdmUgY29udGFpbmVycyAoR2FtZUNo"
    "YW5uZWwudXNlcmxpc3QsIHN0YXRlLmFjdGl2ZVVzZXJzLnZhbHVlcygpLCAuLi4pCiAgICAgICAg"
    "I3RoYXQgb3RoZXIgaGFuZGxlciB0aHJlYWRzIGFwcGVuZCB0by9yZW1vdmUgZnJvbSBjb250aW51"
    "b3VzbHk7IHRoZQogICAgICAgICNkaXN0cmlidXRvciB0aHJlYWQgaXRlcmF0ZWQgdGhlbSBsYXRl"
    "ciBhbmQgaGl0ICdsaXN0IGNoYW5nZWQgc2l6ZQogICAgICAgICNkdXJpbmcgaXRlcmF0aW9uJywg"
    "d2hpY2ggdGhlIGV4Y2VwdCBhYm92ZSBzd2FsbG93ZWQgLSBzaWxlbnRseQogICAgICAgICNkcm9w"
    "cGluZyB0aGUgZW50aXJlIGJyb2FkY2FzdC4gdXBkYXRlUG9zKCkgZG9lcyB0aGlzIG9uY2UgYSBz"
    "ZWNvbmQgZm9yCiAgICAgICAgI2V2ZXJ5IGNoYW5uZWwsIHNvIHRoaXMgd2FzIHRoZSBob3QgcGF0"
    "aCBmb3IgdGhlIHJhY2UuCiAgICAgICAgaWYgaXNpbnN0YW5jZShwcm9wcywgZGljdCk6CiAgICAg"
    "ICAgICAgIHByb3BzID0gZGljdChwcm9wcykKICAgICAgICAgICAgcHJvcHNbJ3RhcmdldCddID0g"
    "bGlzdChwcm9wcy5nZXQoJ3RhcmdldCcpIG9yICgpKQogICAgICAgIHNlbGYuX2NRdWV1ZS5wdXQo"
    "cHJvcHMpCiAgICBkZWYgZW5kKHNlbGYpOgogICAgICAgIHNlbGYuYWRkKHNlbGYuX0VORElURU0p"
    "CiAgICAKZGVmIF9jbGFtcEludChyYXcsIGRlZmF1bHQsIGxvLCBoaSk6CiAgICAjRXZlcnkgbnVt"
    "ZXJpYyBmaWVsZCBvZiAvY3JlYXRlZ2FtZSBhcnJpdmVzIGFzIHRleHQgc3RyYWlnaHQgb2ZmIHRo"
    "ZSB3aXJlLgogICAgI2ludCgpIG9uIGl0IHVzZWQgdG8gcmFpc2UgVmFsdWVFcnJvciBmb3IgYW55"
    "dGhpbmcgbm9uLW51bWVyaWMsIGFuZCB0aGF0CiAgICAjZXhjZXB0aW9uIGxlZnQgdGhlIGhhbmRs"
    "ZXIsIHRvcmUgZG93biB0aGUgaG9zdCdzIGNvbm5lY3Rpb24gdGhyZWFkIGFuZAogICAgI2xvZ2dl"
    "ZCBhIHRyYWNlYmFjayAtIG9uZSBtYWxmb3JtZWQgcm9vbSByZXF1ZXN0IGRpc2Nvbm5lY3RlZCB0"
    "aGUgcGxheWVyCiAgICAjbWFraW5nIGl0LiBUaGUgcmFuZ2UgY2hlY2sgaXMgdGhlIHNhbWUgcmVh"
    "c29uaW5nIGFwcGxpZWQgdG8gdmFsdWVzIHRoYXQgZG8KICAgICNwYXJzZTogbWF4cGxheWVycyBj"
    "YW1lIGZyb20gdGhlIGNsaWVudCB0b28sIHNvIGEgcm9vbSBjb3VsZCBhZHZlcnRpc2UKICAgICNp"
    "dHNlbGYgYXMgaG9sZGluZyB0d28gYmlsbGlvbiBwZW9wbGUuCiAgICB0cnk6CiAgICAgICAgdmFs"
    "ID0gaW50KHJhdykKICAgIGV4Y2VwdCAoVHlwZUVycm9yLCBWYWx1ZUVycm9yKToKICAgICAgICBy"
    "ZXR1cm4gZGVmYXVsdAogICAgcmV0dXJuIG1pbihtYXgodmFsLCBsbyksIGhpKQpjbGFzcyBHYW1l"
    "RW50cnkoKToKICAgIGRlZiBfX2luaXRfXyhzZWxmLCBwYXJlbnQsIG5hbWUsIGhvc3QsIHBhc3cs"
    "IG1hcHAsIG1hcHQsIG5waiwgdW4xLCBzdGF0dXMsIG1heHBsYXllcnMsIHVybCk6CiAgICAgICAg"
    "aWYgaG9zdC51c2VyLmdhbWU6CiAgICAgICAgICAgIGhvc3QudXNlci5nYW1lLnJlbW92ZShob3N0"
    "KQogICAgICAgIHNlbGYucGFyZW50ID0gcGFyZW50ICMgR2FtZWNoYW5uZWwKICAgICAgICBzZWxm"
    "LmduYW1lID0gbmFtZSAjCiAgICAgICAgc2VsZi5ob3N0ID0gaG9zdCAjIENvbm5lY3Rpb24gT2Jq"
    "ZWN0CiAgICAgICAgc2VsZi5wYXNzd29yZCA9IHBhc3cgIyAnJyBvciAncGFzc3dvcmQnCiAgICAg"
    "ICAgc2VsZi5tYXBQYXIgPSBtYXBwICMgIk5ldF9NXzAxIG51bGwgMCAxIgogICAgICAgIHNlbGYu"
    "bWFwVHJhbnNsYXRlID0gbWFwdCAjICJ0cmFuc2xhdGVOZXRfTV8wMSIKICAgICAgICBzZWxmLm5w"
    "aiA9IF9jbGFtcEludChucGosIDAsIDAsIDEpICMgImVuYWJsZSBuZXcgcGxheWVyIHRvIGpvaW4g"
    "KGJvb2wpIgogICAgICAgIHNlbGYudW4xID0gX2NsYW1wSW50KHVuMSwgMCwgMCwgXzMyYml0KSAj"
    "IDAgVE9ETyBmaWd1cmUgb3V0IGlmIG1lYW5zICJndWlsZCBnYW1lIgogICAgICAgIHNlbGYuc3Rh"
    "dHVzID0gX2NsYW1wSW50KHN0YXR1cywgMCwgMCwgMSkgIyBjaGFuZ2VzIHRvIDEgd2hlbiBzdGFy"
    "dGVkLCBvbmx5IHJlbGV2YW50IHdoZW4gbnBqIHRydWUKICAgICAgICBzZWxmLm1heHBsYXllcnMg"
    "PSBfY2xhbXBJbnQobWF4cGxheWVycywgOCwgMSwgR2FtZUNoYW5uZWwubWF4dXNlcikgIyA4ICNt"
    "YXggdXNlcnM/CiAgICAgICAgI3gtZGlyZWN0cGxheSB1cmwsIHdpdGggdGhlIGhvc3QncyBhZHZl"
    "cnRpc2VkIGFkZHJlc3MgcmVwbGFjZWQgYnkgdGhlCiAgICAgICAgI2FkZHJlc3MgdGhpcyBzZXJ2"
    "ZXIgc2VlcyBpdCBjb25uZWN0IGZyb20gLSBzZWUgcmV3cml0ZUdhbWVIb3N0KCkuCiAgICAgICAg"
    "cGVlciA9IGhvc3QuY2xpZW50X2FkZHJlc3NbMF0gaWYgaG9zdC5jbGllbnRfYWRkcmVzcyBlbHNl"
    "ICcnCiAgICAgICAgI0tlcHQgYXMgdGhleSBhcnJpdmVkOiB0aGUgYWRkcmVzcyBhIGpvaW5lciBp"
    "cyBnaXZlbiBpcyBwaWNrZWQgd2hlbgogICAgICAgICN0aGF0IGpvaW5lciBhc2tzLCBmcm9tIGJv"
    "dGggZW5kcyBhdCBvbmNlIC0gc2VlIF91cmxGb3IoKS4gc2VsZi51cmwgaXMKICAgICAgICAjdGhl"
    "IHJvb20ncyBvd24gYmVzdCBhbnN3ZXIgd2l0aCBub2JvZHkgdG8gYWltIGl0IGF0LCB1c2VkIGFz"
    "IHRoZQogICAgICAgICNmYWxsYmFjayBhbmQgYXMgdGhlIHRoaW5nIHRoZSBsb2cgY2FuIHNob3cg"
    "YXQgY3JlYXRpb24gdGltZS4KICAgICAgICBzZWxmLmhvc3RQZWVyID0gcGVlcgogICAgICAgIHNl"
    "bGYucmF3VXJsID0gdXJsCiAgICAgICAgKHNlbGYudXJsLCBub3RlKSA9IHJld3JpdGVHYW1lSG9z"
    "dCh1cmwsIHBlZXIpCiAgICAgICAgcHJpbnQoZidbTG9iYnldIFJvb20gIntuYW1lfSIgYnkge2hv"
    "c3QudXNlci5uYW1lfToge25vdGV9JykKICAgICAgICBwcmludChmJ1tMb2JieV0gICB1cmwgYWR2"
    "ZXJ0aXNlZCB0byBqb2luZXJzOiB7c2VsZi51cmx9JykKICAgICAgICBzZWxmLnVzZXJsaXN0ID0g"
    "W2hvc3QsXQogICAgICAgIHNlbGYucGFyZW50LmdhbWVzW3NlbGYuZ25hbWVdID0gc2VsZgogICAg"
    "ICAgIHNlbGYuaG9zdC51c2VyLmdhbWUgPSBzZWxmCiAgICAgICAgI0FkdmVydGlzZSBvbiBjcmVh"
    "dGlvbgogICAgICAgIG1zZyA9IHNlbGYuZ2V0R2FtZVN0cmluZygpCiAgICAgICAgdGcgPSBzZWxm"
    "LnBhcmVudC51c2VybGlzdAogICAgICAgIHNlbGYucGFyZW50LnNlcnZlci5kaXN0LmFkZCh7J3Rh"
    "cmdldCc6dGcsJ21lc3NhZ2UnOm1zZ30pCiAgICBkZWYgX2F1ZGllbmNlKHNlbGYpOgogICAgICAg"
    "ICNXaG8gbmVlZHMgdG8gaGVhciBhYm91dCB0aGlzIHJvb20gY2hhbmdpbmc6IGV2ZXJ5b25lIGJy"
    "b3dzaW5nIHRoZQogICAgICAgICN0b3duLCBwbHVzIGV2ZXJ5b25lIGFscmVhZHkgaW5zaWRlIHRo"
    "ZSByb29tLiBPbmNlIGEgZ2FtZSBzdGFydHMgaXRzCiAgICAgICAgI3BsYXllcnMgYXJlIHRha2Vu"
    "IG9mZiB0aGUgdG93biByb3N0ZXIgKHNlZSBzdGFydEdhbWUpLCBzbyB0aGUgdG93bgogICAgICAg"
    "ICNsaXN0IGFsb25lIG5vIGxvbmdlciByZWFjaGVzIHRoZW0gLSBhbmQgdGhlIGhvc3QsIHdobyBp"
    "cyBhbHdheXMKICAgICAgICAjaW4tZ2FtZSwgaXMgZXhhY3RseSB3aG8gbmVlZHMgdG8ga25vdyB0"
    "aGF0IHNvbWVib2R5IGpvaW5lZC4KICAgICAgICBzZWVuID0gbGlzdChzZWxmLnBhcmVudC51c2Vy"
    "bGlzdCkKICAgICAgICBmb3IgYyBpbiBzZWxmLnVzZXJsaXN0OgogICAgICAgICAgICBpZiBjIG5v"
    "dCBpbiBzZWVuOgogICAgICAgICAgICAgICAgc2Vlbi5hcHBlbmQoYykKICAgICAgICByZXR1cm4g"
    "c2VlbgogICAgZGVmIF91cmxGb3Ioc2VsZiwgdXNyKToKICAgICAgICAjVGhlIGFkZHJlc3Mgb2Yg"
    "dGhlIGhvc3QgdGhhdCBUSElTIGpvaW5lciBzaG91bGQgYmUgc2VudCB0by4gQm90aCBlbmRzCiAg"
    "ICAgICAgI2FyZSBrbm93biBoZXJlIGFuZCBvbmx5IGhlcmU6IHdoZXJlIHRoZSBob3N0IGNvbm5l"
    "Y3RlZCBmcm9tLCBhbmQgYm90aAogICAgICAgICN3aGVyZSB0aGUgam9pbmVyIGNvbm5lY3RlZCBm"
    "cm9tIGFuZCB3aGljaCBvZiBvdXIgb3duIGFkZHJlc3NlcyB0aGV5CiAgICAgICAgI3JlYWNoZWQg"
    "dXMgYXQgLSBzZWUgcGlja0pvaW5BZGRyZXNzLgogICAgICAgIHBlZXIgPSB1c3IuY2xpZW50X2Fk"
    "ZHJlc3NbMF0gaWYgdXNyLmNsaWVudF9hZGRyZXNzIGVsc2UgJycKICAgICAgICBsb2NhbCA9ICcn"
    "CiAgICAgICAgdHJ5OgogICAgICAgICAgICBsb2NhbCA9IHVzci5yZXF1ZXN0LmdldHNvY2tuYW1l"
    "KClbMF0KICAgICAgICBleGNlcHQgT1NFcnJvcjoKICAgICAgICAgICAgcGFzcyAjc29ja2V0IGFs"
    "cmVhZHkgZ29uZTsgdGhlIHJvb20ncyBvd24gdXJsIGlzIHRoZSBmYWxsYmFjawogICAgICAgICh1"
    "cmwsIG5vdGUpID0gcmV3cml0ZUdhbWVIb3N0Rm9ySm9pbmVyKHNlbGYucmF3VXJsLCBzZWxmLmhv"
    "c3RQZWVyLCBwZWVyLCBsb2NhbCkKICAgICAgICBpZiBub3QgdXJsOgogICAgICAgICAgICByZXR1"
    "cm4gc2VsZi51cmwKICAgICAgICBwcmludChmJ1tMb2JieV0ge3Vzci51c2VyLm5hbWV9IGpvaW5z"
    "ICJ7c2VsZi5nbmFtZX0iOiB7bm90ZX0nKQogICAgICAgIHJldHVybiB1cmwKICAgIGRlZiBhZGRV"
    "c2VyKHNlbGYsIHVzciwgcGFzdyk6CiAgICAgICAgI0V2ZXJ5IHJlamVjdGlvbiBiZWxvdyBoYXMg"
    "dG8gYW5zd2VyIHRoZSBjbGllbnQgd2l0aCAqc29tZXRoaW5nKi4gVGhlCiAgICAgICAgI2NsaWVu"
    "dCBzaG93cyAiY29ubmVjdGluZy4uLiIgZnJvbSB0aGUgbW9tZW50IGl0IHNlbmRzIC9qb2luZ2Ft"
    "ZSB1bnRpbAogICAgICAgICN0aGUgc2VydmVyIGFuc3dlcnMsIGFuZCBpdCBoYXMgbm8gdGltZW91"
    "dCBvZiBpdHMgb3duOiByZXR1cm5pbmcgTm9uZQogICAgICAgICNsZWZ0IHRoZSBwbGF5ZXIgc3Rh"
    "cmluZyBhdCB0aGF0IGRpYWxvZyB1bnRpbCB0aGV5IGtpbGxlZCB0aGUgZ2FtZS4KICAgICAgICBp"
    "ZiB1c3IgaW4gc2VsZi51c2VybGlzdDoKICAgICAgICAgICAgI0FscmVhZHkgaW4gKGR1cGxpY2F0"
    "ZSAvam9pbmdhbWUsIGUuZy4gdGhlIHBsYXllciBkb3VibGUtY2xpY2tlZAogICAgICAgICAgICAj"
    "dGhlIHJvb20pLiBSZS1hbnN3ZXIgaW5zdGVhZCBvZiBhcHBlbmRpbmcgdGhlbSBhIHNlY29uZCB0"
    "aW1lLgogICAgICAgICAgICByZXR1cm4gX2VtKGYnL2pvaW5nYW1lICJ7c2VsZi5nbmFtZX0iICJ7"
    "c2VsZi5fdXJsRm9yKHVzcil9IiAie3NlbGYuc3RhdHVzfSInKQogICAgICAgIGlmIGxlbihzZWxm"
    "LnVzZXJsaXN0KT49c2VsZi5tYXhwbGF5ZXJzOgogICAgICAgICAgICByZXR1cm4gX2VtKGYnL2Vy"
    "cm9yIGdhbWVGdWxsICJ7c2VsZi5nbmFtZX0iJykKICAgICAgICBpZiBzZWxmLnN0YXR1cyBhbmQg"
    "bm90IHNlbGYubnBqOgogICAgICAgICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIGdhbWVBbHJlYWR5"
    "U3RhcnRlZCAie3NlbGYuZ25hbWV9IicpCiAgICAgICAgaWYgc2VsZi5wYXNzd29yZCAhPSBwYXN3"
    "OgogICAgICAgICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIGJhZEdhbWVQYXNzd29yZCAie3NlbGYu"
    "Z25hbWV9IicpCiAgICAgICAgaWYgdXNyLnVzZXIuZ2FtZSBpcyBub3QgTm9uZToKICAgICAgICAg"
    "ICAgdXNyLnVzZXIuZ2FtZS5yZW1vdmUodXNyKSAjbGVhdmUgdGhlIHByZXZpb3VzIHJvb20gY2xl"
    "YW5seSBmaXJzdAogICAgICAgIHNlbGYudXNlcmxpc3QuYXBwZW5kKHVzcikKICAgICAgICB1c3Iu"
    "dXNlci5nYW1lID0gc2VsZgogICAgICAgIHJldCA9IF9lbShmJyRnYW1ldXNlciAie3NlbGYuZ25h"
    "bWV9IiAie3Vzci51c2VyLm5hbWV9IiAiIiAiMTAwIiAiMCInKQogICAgICAgICNVbmNvbmRpdGlv"
    "bmFsbHksIHRvIGV2ZXJ5b25lIGluIHRoZSB0b3duLiBUaGlzIHVzZWQgdG8gYmUgc2VudCBvbmx5"
    "CiAgICAgICAgI3doZW4gbnBqICgibmV3IHBsYXllcnMgbWF5IGpvaW4gYSBydW5uaW5nIGdhbWUi"
    "KSB3YXMgc2V0IC0gYnV0IG5wagogICAgICAgICNzYXlzIG5vdGhpbmcgYWJvdXQgd2hvIHNob3Vs"
    "ZCBoZWFyIGFib3V0IGEgam9pbiwgaXQgb25seSBjb250cm9scwogICAgICAgICN3aGV0aGVyIGEg"
    "KnN0YXJ0ZWQqIGdhbWUgc3RheXMgbGlzdGVkLiBGb3IgYW4gb3JkaW5hcnkgcm9vbSwgd2hpY2gg"
    "aXMKICAgICAgICAjY3JlYXRlZCB3aXRoIG5waj0wIGFuZCBqb2luZWQgYmVmb3JlIGl0IHN0YXJ0"
    "cywgbm9ib2R5IHdhcyBldmVyIHRvbGQ6CiAgICAgICAgI3RoZSBob3N0J3MgbG9iYnkgbmV2ZXIg"
    "bGlzdGVkIHRoZSBhcnJpdmluZyBwbGF5ZXIsIHNvIHRoZSBob3N0IGhhZAogICAgICAgICNub2Jv"
    "ZHkgdG8gc3RhcnQgdGhlIGdhbWUgd2l0aCwgYW5kIHRoZSBqb2luZXIgc2F0IGluICJjb25uZWN0"
    "aW5nIgogICAgICAgICNmb3JldmVyIHdhaXRpbmcgZm9yIGEgc3RhcnQgdGhhdCBjb3VsZCBub3Qg"
    "Y29tZS4KICAgICAgICB1c3Iuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0JzpzZWxmLl9hdWRpZW5j"
    "ZSgpLCdtZXNzYWdlJzpyZXR9KQogICAgICAgICNUaGUgcGFyYW1ldGVycyB0aGlzIHBsYXllciBp"
    "cyBhYm91dCB0byBidWlsZCB0aGUgbWlzc2lvbiBmcm9tLiBMb2dnZWQKICAgICAgICAjb24gZXZl"
    "cnkgam9pbiBiZWNhdXNlIHdoZW4gdHdvIHBsYXllcnMgZGlzYWdyZWUgYWJvdXQgdGhlbSAtIG1v"
    "c3QKICAgICAgICAjdmlzaWJseSBhYm91dCB0aGUgbW9kZSwgd2hpY2ggY2FycmllcyB0aGUgY28t"
    "b3AgZGlmZmljdWx0eSAtIHRoZWlyCiAgICAgICAgI3dvcmxkcyBkaXZlcmdlIHNpbGVudGx5LCBh"
    "bmQgdGhpcyBsaW5lIGlzIHRoZSBvbmx5IHBsYWNlIHRoYXQgc2hvd3MKICAgICAgICAjd2hhdCBl"
    "YWNoIG9mIHRoZW0gd2FzIGFjdHVhbGx5IHRvbGQuCiAgICAgICAgcHJpbnQoZidbTG9iYnldIHt1"
    "c3IudXNlci5uYW1lfSBqb2luZWQgIntzZWxmLmduYW1lfSIgd2l0aCBwYXJhbWV0ZXJzICcKICAg"
    "ICAgICAgICAgICBmJ3tzZWxmLm1hcFBhciFyfScpCiAgICAgICAgcmV0dXJuIF9lbShmJy9qb2lu"
    "Z2FtZSAie3NlbGYuZ25hbWV9IiAie3NlbGYuX3VybEZvcih1c3IpfSIgIntzZWxmLnN0YXR1c30i"
    "JykKICAgIGRlZiBkZXN0cm95KHNlbGYpOgogICAgICAgICNUZWFyIHRoZSByb29tIGRvd24gY29t"
    "cGxldGVseTogZXZlcnlvbmUgc3RpbGwgbGlzdGVkIGluIGl0IGlzIHB1dAogICAgICAgICNiYWNr"
    "IHRvICJub3QgaW4gYSBnYW1lIiwgYW5kIHRoZSByb29tIHN0b3BzIGJlaW5nIGFkdmVydGlzZWQu"
    "CiAgICAgICAgdGcgPSBzZWxmLl9hdWRpZW5jZSgpCiAgICAgICAgZm9yIGMgaW4gbGlzdChzZWxm"
    "LnVzZXJsaXN0KToKICAgICAgICAgICAgaWYgYy51c2VyOgogICAgICAgICAgICAgICAgYy51c2Vy"
    "LmdhbWUgPSBOb25lCiAgICAgICAgc2VsZi51c2VybGlzdCA9IFtdCiAgICAgICAgaWYgc2VsZi5w"
    "YXJlbnQuZ2FtZXMuZ2V0KHNlbGYuZ25hbWUpIGlzIHNlbGY6CiAgICAgICAgICAgIGRlbCBzZWxm"
    "LnBhcmVudC5nYW1lc1tzZWxmLmduYW1lXQogICAgICAgIHNlbGYucGFyZW50LnNlcnZlci5kaXN0"
    "LmFkZCh7J3RhcmdldCc6dGcsCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAn"
    "bWVzc2FnZSc6X2VtKGYnJmdhbWUgIntzZWxmLmduYW1lfSInKX0pCiAgICBkZWYgcmVtb3ZlKHNl"
    "bGYsIGNvbj1Ob25lKTojVE9ETyByZWNyZWF0ZSBwcm9wZXJseQogICAgICAgIGlmIGNvbiBpcyBO"
    "b25lIG9yIGNvbiBub3QgaW4gc2VsZi51c2VybGlzdDoKICAgICAgICAgICAgcmV0dXJuCiAgICAg"
    "ICAgdGcgPSBzZWxmLl9hdWRpZW5jZSgpCiAgICAgICAgc2VsZi51c2VybGlzdC5yZW1vdmUoY29u"
    "KQogICAgICAgIGlmIGNvbi51c2VyIGlzIE5vbmU6CiAgICAgICAgICAgICNDb25uZWN0aW9uIGFs"
    "cmVhZHkgdG9ybiBkb3duIChpdHMgaGFuZGxlciByYW4gZmluaXNoKCkgd2hpbGUgdGhpcwogICAg"
    "ICAgICAgICAjcmVtb3ZhbCB3YXMgb24gaXRzIHdheSB0aHJvdWdoIGFub3RoZXIgdGhyZWFkKS4g"
    "Tm90aGluZyBsZWZ0IHRvCiAgICAgICAgICAgICNhbm5vdW5jZSBhYm91dCBpdCwgYnV0IHRoZSBy"
    "b29tIGl0c2VsZiBzdGlsbCBoYXMgdG8gYmUgdGlkaWVkIHVwCiAgICAgICAgICAgICNiZWxvdywg"
    "c28gZG9uJ3QgcmV0dXJuIGVhcmx5LgogICAgICAgICAgICBsZWF2ZW1zZyA9IGInJwogICAgICAg"
    "IGVsc2U6CiAgICAgICAgICAgIGxlYXZlbXNnID0gX2VtKGYnJmdhbWV1c2VyICJ7Y29uLnVzZXIu"
    "bmFtZX0iJykKICAgICAgICAgICAgY29uLnVzZXIuZ2FtZSA9IE5vbmUKICAgICAgICBpZiBjb24g"
    "aXMgc2VsZi5ob3N0OgogICAgICAgICAgICAjVGhlIGhvc3QgKmlzKiB0aGUgZ2FtZSBzZXNzaW9u"
    "OiB0aGUgY28tb3Agd29ybGQgcnVucyBvbiB0aGVpcgogICAgICAgICAgICAjbWFjaGluZSBhbmQg"
    "dGhlIHJvb20ncyBEaXJlY3RQbGF5IHVybCBwb2ludHMgYXQgaXQuIE9uY2UgdGhleSBhcmUKICAg"
    "ICAgICAgICAgI2dvbmUgdGhlIHJvb20gY2Fubm90IGJlIGpvaW5lZCBieSBhbnlib2R5LCBidXQg"
    "aXQgdXNlZCB0byBzdGF5CiAgICAgICAgICAgICNsaXN0ZWQgLSBzbyB0aGUgbmV4dCBwbGF5ZXIg"
    "dG8gY2xpY2sgaXQgZ290IGEgdXJsIHRvIGEgZ2FtZSB0aGF0CiAgICAgICAgICAgICNubyBsb25n"
    "ZXIgZXhpc3RlZCBhbmQgc2F0IG9uICJjb25uZWN0aW5nIiB1bnRpbCB0aGV5IGdhdmUgdXAuCiAg"
    "ICAgICAgICAgICNUaGlzIGlzIHdoYXQgYSBob3N0IGNyYXNoIGxlYXZlcyBiZWhpbmQuCiAgICAg"
    "ICAgICAgIHdobyA9IGNvbi51c2VyLm5hbWUgaWYgY29uLnVzZXIgZWxzZSAnPycKICAgICAgICAg"
    "ICAgcHJpbnQoZidbTG9iYnldIEhvc3Qge3dob30gbGVmdCByb29tICJ7c2VsZi5nbmFtZX0iLCBj"
    "bG9zaW5nIGl0JykKICAgICAgICAgICAgaWYgbGVhdmVtc2c6CiAgICAgICAgICAgICAgICBzZWxm"
    "LnBhcmVudC5zZXJ2ZXIuZGlzdC5hZGQoeyd0YXJnZXQnOnRnLCdtZXNzYWdlJzpsZWF2ZW1zZ30p"
    "CiAgICAgICAgICAgIHNlbGYuZGVzdHJveSgpCiAgICAgICAgICAgIHJldHVybgogICAgICAgICNp"
    "ZiAwIHVzZXJzIGxlZnQsIHJlbW92ZSBnYW1lCiAgICAgICAgaWYgbGVuKHNlbGYudXNlcmxpc3Qp"
    "PT0wOgogICAgICAgICAgICBsZWF2ZW1zZyA9IF9lbShmJyZnYW1lICJ7c2VsZi5nbmFtZX0iJykK"
    "ICAgICAgICAgICAgI09ubHkgaWYgdGhpcyBlbnRyeSBpcyBzdGlsbCB0aGUgb25lIHJlZ2lzdGVy"
    "ZWQgdW5kZXIgdGhhdCBuYW1lLiBBCiAgICAgICAgICAgICNyb29tIHdob3NlIGhvc3QgcmVjb25u"
    "ZWN0cyBhbmQgcmUtaG9zdHMgaXMgcmVwbGFjZWQgYnkgYSAqbmV3KgogICAgICAgICAgICAjR2Ft"
    "ZUVudHJ5IHdpdGggdGhlIHNhbWUgbmFtZSAoc2VlIF9pc1N0YWxlR2FtZSk7IHRoZSBvbGQgb25l"
    "J3MKICAgICAgICAgICAgI2xhc3QgcGxheWVyIGxlYXZpbmcgdGhlbiBkZWxldGVkIHRoZSBsaXZl"
    "IHJvb20gb3V0IG9mIHRoZSBjaGFubmVsIC0KICAgICAgICAgICAgI29yIHJhaXNlZCBLZXlFcnJv"
    "ciBpZiBpdCBoYWQgYWxyZWFkeSBnb25lLCBpbnNpZGUgdGhlIGRpc2Nvbm5lY3QKICAgICAgICAg"
    "ICAgI3BhdGgsIHdoaWNoIGFib3J0cyB0aGUgcmVzdCBvZiB0aGF0IHBsYXllcidzIGNsZWFudXAu"
    "CiAgICAgICAgICAgIGlmIHNlbGYucGFyZW50LmdhbWVzLmdldChzZWxmLmduYW1lKSBpcyBzZWxm"
    "OgogICAgICAgICAgICAgICAgZGVsIHNlbGYucGFyZW50LmdhbWVzW3NlbGYuZ25hbWVdCiAgICAg"
    "ICAgaWYgbGVhdmVtc2c6CiAgICAgICAgICAgIHNlbGYucGFyZW50LnNlcnZlci5kaXN0LmFkZCh7"
    "J3RhcmdldCc6dGcsJ21lc3NhZ2UnOmxlYXZlbXNnfSkKICAgIGRlZiBzdGFydEdhbWUoc2VsZiwg"
    "dXNlcj1Ob25lKToKICAgICAgICBpZiBub3QgKHVzZXIgYW5kIHNlbGYuaG9zdCA9PSB1c2VyKToK"
    "ICAgICAgICAgICAgcmV0dXJuIE5vbmUgI3VzZXIgbm90IGhvc3QKICAgICAgICB0ZyA9IHNlbGYu"
    "X2F1ZGllbmNlKCkKICAgICAgICBzZWxmLnN0YXR1cyA9IDEKICAgICAgICBmb3IgYyBpbiBzZWxm"
    "LnVzZXJsaXN0OiNUT0RPIGhhdmUgdXNlciByZW1vdmUgaXRzZWxmIHdoZW4gL3N0YXJ0aW5nZ2Ft"
    "ZT8KICAgICAgICAgICAgdW4gPSBjLnVzZXIubmFtZQogICAgICAgICAgICAjVE9ETyBjb25zaWRl"
    "ciByZW1vdmluZyB1c2VyIGZyb20gdGFyZ2V0IG93biBzZXQ/CiAgICAgICAgICAgIHNlbGYucGFy"
    "ZW50LnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6dGcsJ21lc3NhZ2UnOl9lbShmJyZjaGF0Y2hh"
    "bm5lbHVzZXIgInt1bn0iJykrX2VtKGYnJmdhbWVjaGFubmVsdXNlciAie3VufSInKX0pCiAgICAg"
    "ICAgIy4uLmFuZCBhY3R1YWxseSB0YWtlIHRoZW0gb2ZmIHRoZSB0b3duIHJvc3Rlciwgd2hpY2gg"
    "dGhpcyBvbmx5IGV2ZXIKICAgICAgICAjKmFubm91bmNlZCouIExlYXZpbmcgdGhlbSBsaXN0ZWQg"
    "bWVhbnQgdGhlIHNlcnZlciBzdGlsbCBjb3VudGVkIHRoZW0KICAgICAgICAjYXMgc3RhbmRpbmcg"
    "aW4gdGhlIHRvd24gZm9yIHRoZSB3aG9sZSBzZXNzaW9uOiB0b3duIHBvcHVsYXRpb24gd2FzCiAg"
    "ICAgICAgI3dyb25nLCBhbmQgZXZlcnkgcG9zaXRpb24gdXBkYXRlIGZyb20gYW55b25lIHN0aWxs"
    "IHdhbGtpbmcgYXJvdW5kIHdhcwogICAgICAgICNmYW5uZWQgb3V0IHRvIHBsYXllcnMgd2hvIHdl"
    "cmUgYXdheSBpbiBhIGNvLW9wIHdvcmxkIGFuZCBjb3VsZCBkbwogICAgICAgICNub3RoaW5nIHdp"
    "dGggaXQuIFRoZSBjbGllbnRzIHdlcmUgdG9sZCB0aGV5IGxlZnQ7IG5vdyB0aGUgc2VydmVyCiAg"
    "ICAgICAgI2FncmVlcyB3aXRoIHRoZW0uCiAgICAgICAgZm9yIGMgaW4gbGlzdChzZWxmLnVzZXJs"
    "aXN0KToKICAgICAgICAgICAgYy51c2VyLmxlYXZlQ2hhdCgpCiAgICAgICAgICAgIGlmIGMgaW4g"
    "c2VsZi5wYXJlbnQudXNlcmxpc3Q6CiAgICAgICAgICAgICAgICBzZWxmLnBhcmVudC51c2VybGlz"
    "dC5yZW1vdmUoYykKICAgICAgICBpZiBub3Qgc2VsZi5ucGo6CiAgICAgICAgICAgICNnYW1lIG5v"
    "IGxvbmdlciBqb2luYWJsZS92aXNpYmxlIG9uY2Ugc3RhcnRlZAogICAgICAgICAgICBzZWxmLnBh"
    "cmVudC5zZXJ2ZXIuZGlzdC5hZGQoeyd0YXJnZXQnOnRnLCdtZXNzYWdlJzpfZW0oZicmZ2FtZSAi"
    "e3NlbGYuZ25hbWV9IicpfSkKICAgICAgICAjbm90aWZ5IHBsYXllcnMgaW4gdGhlIGdhbWUgdGhh"
    "dCBpdCBoYXMgc3RhcnRlZAogICAgICAgIGZvciBjIGluIHNlbGYudXNlcmxpc3Q6CiAgICAgICAg"
    "ICAgIGlzSG9zdCA9IDEgaWYgYyBpcyBzZWxmLmhvc3QgZWxzZSAwCiAgICAgICAgICAgIHNlbGYu"
    "cGFyZW50LnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6KGMsKSwnbWVzc2FnZSc6X2VtKGYnL3N0"
    "YXJ0Z2FtZSAiMSIgIntpc0hvc3R9IiAiMSInKX0pCiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGRl"
    "ZiBfZ2V0VXNlcmxpc3Qoc2VsZik6CiAgICAgICAgcmV0dXJuICcgJy5qb2luKCAoZicie2MudXNl"
    "ci5uYW1lfSIgIiIgIjEwMCIgIjAiJyBmb3IgYyBpbiBzZWxmLnVzZXJsaXN0KSApCiAgICBkZWYg"
    "Z2V0R2FtZVN0cmluZyhzZWxmKToKICAgICAgICBpZiBzZWxmLnN0YXR1cyBhbmQgbm90IHNlbGYu"
    "bnBqOgogICAgICAgICAgICByZXR1cm4gTm9uZSAjR2FtZSBkb2VzIG5vdCBzaG93IGlmIG5ldyBw"
    "bGF5ZXJzIGNhbid0IGpvaW4gd2hlbiBhY3RpdmUKICAgICAgICBwYXN3ID0gJycKICAgICAgICBp"
    "ZiBzZWxmLnBhc3N3b3JkOgogICAgICAgICAgICBwYXN3ID0gJ1hYWCcKICAgICAgICByZXR1cm4g"
    "X2VtKGYnJGdhbWUgIntzZWxmLmduYW1lfSIgIntwYXN3fSIgIntzZWxmLm1hcFBhcn0iICJ7c2Vs"
    "Zi5tYXBUcmFuc2xhdGV9IiAie3NlbGYudW4xfSIgIntzZWxmLnN0YXR1c30iICJ7c2VsZi5tYXhw"
    "bGF5ZXJzfSIge3NlbGYuX2dldFVzZXJsaXN0KCl9JykKICAgIGRlZiBkZWJ1Z19kaWN0KHNlbGYp"
    "OgogICAgICAgIHJldHVybiB7CiAgICAgICAgICAgICduYW1lJzpzZWxmLmduYW1lLAogICAgICAg"
    "ICAgICAnaG9zdCc6c2VsZi5ob3N0LnVzZXIubmFtZSwKICAgICAgICAgICAgJ3N0YXR1cyc6c2Vs"
    "Zi5zdGF0dXMsCiAgICAgICAgICAgICdoYXNQYXNzd29yZCc6MSBpZiBzZWxmLnBhc3N3b3JkIGVs"
    "c2UgMCwKICAgICAgICAgICAgJ3VzZXJzJzp0dXBsZShbYy51c2VyLm5hbWUgZm9yIGMgaW4gc2Vs"
    "Zi51c2VybGlzdF0pLAogICAgICAgICAgICAndG93bic6c2VsZi5wYXJlbnQubmFtZSwKICAgICAg"
    "ICAgICAgJ3BhcmFtZXRlcnMnOnNlbGYubWFwUGFyLAogICAgICAgICAgICAnbWFwTmFtZSc6c2Vs"
    "Zi5tYXBUcmFuc2xhdGUsCiAgICAgICAgICAgICdjYW5Kb2luUnVubmluZyc6c2VsZi5ucGoKICAg"
    "ICAgICB9CiMgdHJhbnNsYXRlTmV0Q2l0eU1haW5DaGFubmVsCiMgdHJhbnNsYXRlTmV0Q2l0eVRy"
    "YWRlQ2hhbm5lbAojIHRyYW5zbGF0ZU5ldENpdHlDaGF0Q2hhbm5lbApfREVGQVVMVF9DSEFUUyA9"
    "IFsndHJhbnNsYXRlTmV0Q2l0eU1haW5DaGFubmVsJywndHJhbnNsYXRlTmV0Q2l0eVRyYWRlQ2hh"
    "bm5lbCddCmNsYXNzIEdhbWVDaGFubmVsKCk6CiAgICBtYXh1c2VyID0gNTAgI1RPRE8gY29uZmln"
    "dXJlYWJsZQogICAgZGVmIF9faW5pdF9fKHNlbGYsIHNlcnZlciwgY2huTmFtZSk6CiAgICAgICAg"
    "c2VsZi5zZXJ2ZXIgPSBzZXJ2ZXIKICAgICAgICBzZWxmLm5hbWUgPSBjaG5OYW1lCiAgICAgICAg"
    "c2VsZi51c2VybGlzdCA9IFtdCiAgICAgICAgc2VsZi5jaGF0Q2hhbm5lbHMgPSB7fQogICAgICAg"
    "IHNlbGYuZ2FtZXMgPSB7fSAjVE9ETyBmaWd1cmUgb3V0IEEgYW5kIEIgdmFsdWUgZm9yIGRpc3Bs"
    "YXkKICAgICAgICAjVE9ETyByZXF1ZXN0IGpvaW4gcmVzZXJ2ZXMgc3BhY2Ugd2l0aCB3ZWFrIHJl"
    "ZmVyZW5jZXMKICAgICAgICAjLSB3ZWFrIHZhbHVlIHJlZiBzaG91bGQgZW5zdXJlIHRoYXQgY29u"
    "bmVjdGlvbiBpcyByZW1vdmVkIGZyb20gcXVldWUgaWYgaXQgZGlzY29ubmVjdHMgZHVyaW5nIHRo"
    "ZSBqb2luIHByb2Nlc3MKICAgICAgICBzZWxmLnJlcXVlc3RlZCA9IFtdCiAgICAgICAgc2VsZi5n"
    "YW1lUmVxdWVzdHMgPSB7fQogICAgICAgIHNlbGYuZGlydHkgPSBGYWxzZQogICAgICAgIGZvciBj"
    "biBpbiBfREVGQVVMVF9DSEFUUzoKICAgICAgICAgICAgc2VsZi5jaGF0Q2hhbm5lbHNbY25dID0g"
    "W10gI1VzZXJsaXN0CiAgICBkZWYgcmVxdWVzdEpvaW4oc2VsZiwgY29uKToKICAgICAgICAjbGVh"
    "dmVDaGFubmVsKCkgYWxyZWFkeSByZWxlYXNlcyBhbnkgb3V0c3RhbmRpbmcgcmVzZXJ2YXRpb24s"
    "IG9uIHRoaXMKICAgICAgICAjY2hhbm5lbCBvciBhbm90aGVyIG9uZS4gVGhlIGZvbGxvdy11cCBi"
    "bG9jayB0aGF0IHVzZWQgdG8gc3RhbmQgaGVyZQogICAgICAgICNjb3VsZCB0aGVyZWZvcmUgbmV2"
    "ZXIgcnVuIC0gYW5kIGlmIGl0IGV2ZXIgaGFkLCBpdHMgdW5ndWFyZGVkCiAgICAgICAgI2xpc3Qu"
    "cmVtb3ZlKCkgd291bGQgaGF2ZSByYWlzZWQgVmFsdWVFcnJvciBmb3IgYSByZXNlcnZhdGlvbiB0"
    "aGF0IHdhcwogICAgICAgICNhbHJlYWR5IGdvbmUuCiAgICAgICAgY29uLnVzZXIubGVhdmVDaGFu"
    "bmVsKCkKICAgICAgICBlbGVuID0gbGVuKHNlbGYudXNlcmxpc3QpK2xlbihzZWxmLnJlcXVlc3Rl"
    "ZCkKICAgICAgICBpZiBlbGVuPHNlbGYubWF4dXNlcjoKICAgICAgICAgICAgc2VsZi5yZXF1ZXN0"
    "ZWQuYXBwZW5kKGNvbikKICAgICAgICAgICAgY29uLnVzZXIucmVxdWVzdGVkQ2hhbm5lbCA9IHNl"
    "bGYKICAgICAgICAgICAgcmV0dXJuIFRydWUKICAgICAgICByZXR1cm4gRmFsc2UKICAgIGRlZiBf"
    "aXNTdGFsZUdhbWUoc2VsZiwgZ2VudCwgY29uKToKICAgICAgICAjQSByb29tIHdob3NlIGhvc3Qg"
    "aXMgbm8gbG9uZ2VyIHRoZSBsaXZlIHNlc3Npb24gZm9yIHRoYXQgYWNjb3VudC4gVGhlCiAgICAg"
    "ICAgI2NsaWVudCBuYW1lcyBhIHJvb20gYWZ0ZXIgaXRzIGhvc3QsIHNvIHdoZW4gYSBwbGF5ZXIg"
    "d2hvc2UgZ2FtZQogICAgICAgICNjcmFzaGVkIHJlY29ubmVjdHMgYW5kIGhvc3RzIGFnYWluLCB0"
    "aGUgcm9vbSBmcm9tIHRoZSBzZXNzaW9uIHRoYXQKICAgICAgICAjZGllZCBpcyBzdGlsbCBzaXR0"
    "aW5nIGhlcmUgdW5kZXIgdGhlIHNhbWUgbmFtZSAtIHdpdGggYSBob3N0CiAgICAgICAgI2Nvbm5l"
    "Y3Rpb24gdGhhdCBubyBsb25nZXIgZXhpc3RzIGFuZCBhIERpcmVjdFBsYXkgdXJsIHBvaW50aW5n"
    "IGF0IGEKICAgICAgICAjZ2FtZSB0aGF0IGlzIGdvbmUuIEFueW9uZSBqb2luaW5nIGl0IHdhaXRz"
    "IGZvcmV2ZXIuCiAgICAgICAgaWYgZ2VudC5ob3N0IGlzIGNvbjoKICAgICAgICAgICAgcmV0dXJu"
    "IFRydWUKICAgICAgICBob3N0bmFtZSA9IGdlbnQuaG9zdC51c2VyLm5hbWUgaWYgZ2VudC5ob3N0"
    "LnVzZXIgZWxzZSBOb25lCiAgICAgICAgaWYgaG9zdG5hbWUgaXMgTm9uZToKICAgICAgICAgICAg"
    "cmV0dXJuIFRydWUKICAgICAgICByZXR1cm4gc2VsZi5zZXJ2ZXIuZ2V0UGxheWVyKGhvc3RuYW1l"
    "KSBpcyBub3QgZ2VudC5ob3N0CiAgICBkZWYgcmVxdWVzdENyZWF0ZUdhbWUoc2VsZiwgY29uLCBn"
    "YW1lTmFtZSk6CiAgICAgICAgI05ldmVyIHJldHVybiBhIGJhcmUgRmFsc2UgZnJvbSBoZXJlLiBw"
    "YXJzZSgpIHRyZWF0cyBhIGZhbHN5IHJlc3VsdCBhcwogICAgICAgICMibm90aGluZyB0byBzZW5k"
    "Iiwgc28gZXZlcnkgcmVqZWN0aW9uIGJlbG93IHVzZWQgdG8gbGVhdmUgdGhlIGNsaWVudAogICAg"
    "ICAgICN3YWl0aW5nIG9uIGFuIGFuc3dlciB0aGF0IG5ldmVyIGNhbWUgLSB0aGUgcm9vbS1jcmVh"
    "dGlvbiBkaWFsb2cgdGhlbgogICAgICAgICNzcGlucyBmb3JldmVyLgogICAgICAgIGlmIGNvbi51"
    "c2VyLnJlcXVlc3RlZEdhbWUgb3IgY29uLnVzZXIuZ2FtZToKICAgICAgICAgICAgY29uLnVzZXIu"
    "c3RvcEdhbWUoKQogICAgICAgIHRjbiA9IHNlbGYuZ2FtZVJlcXVlc3RzLmdldChnYW1lTmFtZSkK"
    "ICAgICAgICBpZiB0Y24gaXMgbm90IE5vbmUgYW5kIHRjbiBpcyBub3QgY29uOgogICAgICAgICAg"
    "ICByZXR1cm4gX2VtKGYnL2Vycm9yIGdhbWVOYW1lVGFrZW4gIntnYW1lTmFtZX0iJykKICAgICAg"
    "ICAgICAgI2Vsc2UgdGNuIGlzIGNvbiwgcmUtcmVxdWVzdGVkIGNyZWF0aW9uCiAgICAgICAgZ2Vu"
    "dCA9IHNlbGYuZ2FtZXMuZ2V0KGdhbWVOYW1lKQogICAgICAgIGlmIGdlbnQgaXMgbm90IE5vbmU6"
    "CiAgICAgICAgICAgIGlmIHNlbGYuX2lzU3RhbGVHYW1lKGdlbnQsIGNvbik6CiAgICAgICAgICAg"
    "ICAgICBwcmludChmJ1tMb2JieV0gUmVwbGFjaW5nIHN0YWxlIHJvb20gIntnYW1lTmFtZX0iICcK"
    "ICAgICAgICAgICAgICAgICAgICAgIGYnKGhvc3Qgc2Vzc2lvbiBnb25lKSBhdCB0aGUgcmVxdWVz"
    "dCBvZiB7Y29uLnVzZXIubmFtZX0nKQogICAgICAgICAgICAgICAgZ2VudC5kZXN0cm95KCkKICAg"
    "ICAgICAgICAgZWxzZToKICAgICAgICAgICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgZ2FtZU5h"
    "bWVUYWtlbiAie2dhbWVOYW1lfSInKQogICAgICAgIHNlbGYuZ2FtZVJlcXVlc3RzW2dhbWVOYW1l"
    "XSA9IGNvbgogICAgICAgIGNvbi51c2VyLnJlcXVlc3RlZEdhbWUgPSBnYW1lTmFtZQogICAgICAg"
    "IHJldHVybiBfZW0oZicvY3JlYXRlZ2FtZSAie2dhbWVOYW1lfSInKQogICAgZGVmIGNyZWF0ZUdh"
    "bWUoc2VsZiwgZ2FtZU5hbWUsIGhvc3QsIHBhc3csIG1hcHAsIG1hcHQsIG5waiwgdW4xLCB1bjIs"
    "IHVuMywgdXJsKToKICAgICAgICByZXFIb3N0ID0gc2VsZi5nYW1lUmVxdWVzdHMuZ2V0KGdhbWVO"
    "YW1lKQogICAgICAgIGlmIHJlcUhvc3QgaXMgTm9uZSBvciByZXFIb3N0IGlzIG5vdCBob3N0Ogog"
    "ICAgICAgICAgICAjU2FtZSByZWFzb25pbmcgYXMgYWJvdmU6IGFuc3dlciwgbmV2ZXIgZmFsbCBz"
    "aWxlbnQuCiAgICAgICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgZ2FtZU5hbWVUYWtlbiAie2dh"
    "bWVOYW1lfSInKQogICAgICAgIGdlbnQgPSBHYW1lRW50cnkoc2VsZiwgZ2FtZU5hbWUsIGhvc3Qs"
    "IHBhc3csIG1hcHAsIG1hcHQsIG5waiwgdW4xLCB1bjIsIHVuMywgdXJsKQogICAgICAgIHJlcUhv"
    "c3QudXNlci5yZXF1ZXN0ZWRHYW1lID0gTm9uZSAjVE9ETyByZW9nYW5pemUgYmV0dGVyCiAgICAg"
    "ICAgZGVsIHNlbGYuZ2FtZVJlcXVlc3RzW2dhbWVOYW1lXQogICAgICAgIHJldHVybiBOb25lCiAg"
    "ICBkZWYgbGVhdmVDaGFubmVsKHNlbGYsIGNvbik6CiAgICAgICAgI1RoZSBjbGVhbnVwIHJ1bnMg"
    "d2hldGhlciBvciBub3QgdGhlIHBsYXllciBpcyBzdGlsbCBvbiB0aGUgdG93bgogICAgICAgICNy"
    "b3N0ZXIuIFNpbmNlIHN0YXJ0R2FtZSgpIHRha2VzIGl0cyBwbGF5ZXJzIG9mZiB0aGF0IHJvc3Rl"
    "ciwgYQogICAgICAgICNwbGF5ZXIgd2hvIGxlYXZlcyAob3IgZGlzY29ubmVjdHMpIGZyb20gaW5z"
    "aWRlIGEgcnVubmluZyBnYW1lIHVzZWQgdG8KICAgICAgICAjc2tpcCBhbGwgb2YgdGhpczogdGhl"
    "aXIgcm9vbSB3YXMgbmV2ZXIgbGVmdCwgdGhlaXIgY2hhdCBjaGFubmVsIGtlcHQKICAgICAgICAj"
    "dGhlaXIgZW50cnksIGFuZCBnYW1lY2hhbm5lbCBzdGF5ZWQgcG9pbnRpbmcgYXQgYSB0b3duIHRo"
    "ZXkgd2VyZSBubwogICAgICAgICNsb25nZXIgaW4uIE9ubHkgdGhlIHJvc3RlciByZW1vdmFsIGFu"
    "ZCB0aGUgYW5ub3VuY2VtZW50IGFyZQogICAgICAgICNjb25kaXRpb25hbCBub3cgLSBiZWNhdXNl"
    "IG9ubHkgdGhvc2UgZGVwZW5kIG9uIGJlaW5nIGxpc3RlZC4KICAgICAgICBsaXN0ZWQgPSBjb24g"
    "aW4gc2VsZi51c2VybGlzdAogICAgICAgIGNvbi51c2VyLnN0b3BHYW1lKCkKICAgICAgICBjb24u"
    "dXNlci5sZWF2ZUNoYXQoKQogICAgICAgIGlmIGxpc3RlZDoKICAgICAgICAgICAgc2VsZi51c2Vy"
    "bGlzdC5yZW1vdmUoY29uKQogICAgICAgICAgICBsZWF2ZW1zZyA9IF9lbShmJyZnYW1lY2hhbm5l"
    "bHVzZXIgIntjb24udXNlci5uYW1lfSInKQogICAgICAgICAgICBjb24uc2VydmVyLmRpc3QuYWRk"
    "KHsndGFyZ2V0JzpzZWxmLnVzZXJsaXN0LCdtZXNzYWdlJzpsZWF2ZW1zZ30pCiAgICAgICAgY29u"
    "LnVzZXIuZ2FtZWNoYW5uZWw9Tm9uZQogICAgZGVmIGxlYXZlQ2hhdChzZWxmLCBjb24pOiAjVE9E"
    "TyBiZXR0ZXIgY2hhdGNoYW5uZWwgb2JqZWN0IGFuZCBtb3ZlIGl0IHRoZXJlLgogICAgICAgIGNv"
    "bi51c2VyLmxlYXZlQ2hhdCgpCiAgICAjVE9ETyBjaGFuZ2UgdGhlc2UgZnVuY3Rpb25zIHRvIGFs"
    "c28gaGFuZGxlIG1lc3NhZ2UgZm9ybWluZwogICAgZGVmIGpvaW5DaGFubmVsKHNlbGYsIGNvbiwg"
    "bmFtKTojbW92ZXMgdXNlciBmcm9tIHF1ZXVlIHRvIHVzZXJsaXN0CiAgICAgICAgaWYgY29uIGlu"
    "IHNlbGYudXNlcmxpc3Q6CiAgICAgICAgICAgICNEdXBsaWNhdGUgL2pvaW5nYW1lY2hhbm5lbCBm"
    "b3IgYSB0b3duIHdlIGFyZSBhbHJlYWR5IGluLiBSZWJ1aWxkCiAgICAgICAgICAgICN0aGUgcmVz"
    "ZXJ2YXRpb24gc28gdGhlIHJlcXVlc3QgYmVsb3cgcmUtcnVucyB0aGUgZnVsbCBlbnVtZXJhdGlv"
    "bgogICAgICAgICAgICAjYW5kIHRoZSBjbGllbnQgZ2V0cyBhIGNvbXBsZXRlIGFuc3dlciByYXRo"
    "ZXIgdGhhbiBzaWxlbmNlLgogICAgICAgICAgICBzZWxmLnVzZXJsaXN0LnJlbW92ZShjb24pCiAg"
    "ICAgICAgICAgIHNlbGYucmVxdWVzdGVkLmFwcGVuZChjb24pCiAgICAgICAgICAgIGNvbi51c2Vy"
    "LnJlcXVlc3RlZENoYW5uZWwgPSBzZWxmCiAgICAgICAgaWYgY29uIG5vdCBpbiBzZWxmLnJlcXVl"
    "c3RlZCBhbmQgY29uIG5vdCBpbiBzZWxmLnVzZXJsaXN0OgogICAgICAgICAgICAjTm8gb3V0c3Rh"
    "bmRpbmcgcmVzZXJ2YXRpb24uIFRoZSByZXNlcnZhdGlvbiBpcyBkcm9wcGVkIGJ5IGFueQogICAg"
    "ICAgICAgICAjaW50ZXJ2ZW5pbmcgbGVhdmVDaGFubmVsKCkvcmVxdWVzdEpvaW4oKSBhbmQgYnkg"
    "YSByZWNvbm5lY3QsIHNvIGEKICAgICAgICAgICAgI2NsaWVudCB0aGF0IGdvZXMgc3RyYWlnaHQg"
    "dG8gL2pvaW5nYW1lY2hhbm5lbCAtIG9yIHdob3NlIGVhcmxpZXIKICAgICAgICAgICAgIy9yZXF1"
    "ZXN0am9pbmdhbWVjaGFubmVsIHJhY2VkIGl0cyBvd24gY2xlYW51cCAtIHVzZWQgdG8gZ2V0IG5v"
    "CiAgICAgICAgICAgICNhbnN3ZXIgYXQgYWxsIGFuZCBoYW5nIG9uIHRoZSBsb2FkaW5nIHNjcmVl"
    "bi4gQWRtaXQgdGhlbSBpZiB0aGUKICAgICAgICAgICAgI3Rvd24gaGFzIHJvb207IG9ubHkgYSBn"
    "ZW51aW5lbHkgZnVsbCB0b3duIGlzIHJlZnVzZWQgbm93LgogICAgICAgICAgICBpZiBsZW4oc2Vs"
    "Zi51c2VybGlzdCkrbGVuKHNlbGYucmVxdWVzdGVkKSA8IHNlbGYubWF4dXNlcjoKICAgICAgICAg"
    "ICAgICAgIHNlbGYucmVxdWVzdGVkLmFwcGVuZChjb24pCiAgICAgICAgICAgICAgICBjb24udXNl"
    "ci5yZXF1ZXN0ZWRDaGFubmVsID0gc2VsZgogICAgICAgICAgICBlbHNlOgogICAgICAgICAgICAg"
    "ICAgcmV0dXJuIF9lbShmJy9lcnJvciBnYW1lQ2hhbm5lbEZ1bGwgIntuYW19IicpCiAgICAgICAg"
    "aWYgY29uIGluIHNlbGYucmVxdWVzdGVkOgogICAgICAgICAgICAjVE9ETyB2ZXJpZnkgb3JkZXIg"
    "b2Ygb3BlcmF0aW9ucyBhbmQgcG9zc2libGUgdGltaW5nIGlzc3VlcwogICAgICAgICAgICBzZWxm"
    "LnVzZXJsaXN0LmFwcGVuZChjb24pCiAgICAgICAgICAgIGNvbi51c2VyLmdhbWVjaGFubmVsID0g"
    "c2VsZgogICAgICAgICAgICBzZWxmLnJlcXVlc3RlZC5yZW1vdmUoY29uKQogICAgICAgICAgICBj"
    "b24udXNlci5yZXF1ZXN0ZWRDaGFubmVsID0gTm9uZSAjVE9ETyBvcmdhbml6ZSBiZXR0ZXI/CiAg"
    "ICAgICAgICAgIHVsID0gbGVuKHNlbGYudXNlcmxpc3QpCiAgICAgICAgICAgIHJldG1zZyA9IF9l"
    "bShmJy9qb2luZ2FtZWNoYW5uZWwgIntuYW19IiAie3VsfSInKQogICAgICAgICAgICAjZW51bWVy"
    "YXRlIGhlcm9kYXRhIG9mIGV4aXN0aW5nIHVzZXJzCiAgICAgICAgICAgIGNodW5rcyA9IFtdCiAg"
    "ICAgICAgICAgIGZvciB1c2VyIGluIHNlbGYudXNlcmxpc3Q6CiAgICAgICAgICAgICAgICBpZiB1"
    "c2VyID09IGNvbjoKICAgICAgICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICAgICAg"
    "Y2h1bmtzLmFwcGVuZCh1c2VyLnVzZXIuZ2V0R0NVbXNnKCkpCiAgICAgICAgICAgIHJldG1zZys9"
    "IGInJy5qb2luKGNodW5rcykKICAgICAgICAgICAgcmV0bXNnKz0gc2VsZi5qb2luQ2hhdChjb24s"
    "IF9ERUZBVUxUX0NIQVRTWzBdKQogICAgICAgICAgICByZXRtc2crPSBzZWxmLmVudW1DaGF0cygp"
    "CiAgICAgICAgICAgIHJldG1zZys9IHNlbGYuZW51bUdhbWVzKCkKICAgICAgICAgICAgI2Jyb2Fk"
    "Y2FzdCBoZXJvZGF0YSB0byBvdGhlciBleGlzdGluZyB1c2VycwogICAgICAgICAgICBjb24uc2Vy"
    "dmVyLmRpc3QuYWRkKHsKICAgICAgICAgICAgICAgICd0YXJnZXQnOl93b1VzZXIoc2VsZi51c2Vy"
    "bGlzdCwgY29uKSwKICAgICAgICAgICAgICAgICdtZXNzYWdlJzpjb24udXNlci5nZXRHQ1Vtc2co"
    "KX0pCiAgICAgICAgICAgIHJldHVybiByZXRtc2cKICAgICAgICByZXR1cm4gTm9uZQogICAgZGVm"
    "IGpvaW5DaGF0KHNlbGYsIGNvbiwgbmFtLCBwYXM9JycpOgogICAgICAgICNUT0RPIHBhc3N3b3Jk"
    "IHN1cHBvcnQ/CiAgICAgICAgIy0gcmVxdWlyZXMgcmVzdHJ1Y3R1cmUgZnJvbSBsaXN0IHRvIGNo"
    "YW5uZWwgb2JqZWN0cwogICAgICAgIGlmIG5vdCBuYW0gaW4gc2VsZi5jaGF0Q2hhbm5lbHM6CiAg"
    "ICAgICAgICAgIHJldHVybiBiJycKICAgICAgICBjb24udXNlci5sZWF2ZUNoYXQoKQogICAgICAg"
    "ICNUT0RPIGNoZWNrIGlmIGNsaWVudCBhdXRvLXB1cmdlcyBjaGF0bGlzdAogICAgICAgICNGdWxs"
    "IGZvdXItZmllbGQgZm9ybSAobmFtZSwgZ3VpbGQsIGZsYWdzLCBndWlkKSwgd2hpY2ggaXMgd2hh"
    "dCB0aGUKICAgICAgICAjY2xpZW50IGlzIGRvY3VtZW50ZWQgdG8gc2VuZCBhbmQgd2hhdCBnZXRD"
    "Q1Vtc2coKSBleGlzdHMgdG8gYnVpbGQgLQogICAgICAgICNzZWUgdGhlIGNhcHR1cmUgbm90ZWQg"
    "bmV4dCB0byBpdC4gQm90aCBhbm5vdW5jZW1lbnRzIGhlcmUgdXNlZCB0byBlbWl0CiAgICAgICAg"
    "I2Egb25lLWZpZWxkICckY2hhdGNoYW5uZWx1c2VyICJuYW1lIicgaW5zdGVhZCwgc28gdGhlIGd1"
    "aWxkIGNvbHVtbiB3YXMKICAgICAgICAjYWx3YXlzIGJsYW5rIGluIGNoYXQgbm8gbWF0dGVyIHdo"
    "YXQgZ3VpbGQgYSBwbGF5ZXIgd2FzIGluLCBhbmQgdGhlCiAgICAgICAgI2NsaWVudCBoYWQgdG8g"
    "ZmlsbCB0aHJlZSBmaWVsZHMgaXQgd2FzIG5ldmVyIGdpdmVuLiBUaGUgJGdhbWVjaGFubmVsdXNl"
    "cgogICAgICAgICNwYXRoIG5leHQgZG9vciBoYXMgYWx3YXlzIHNlbnQgaXRzIGZ1bGwgZm9ybTsg"
    "dGhlc2UgdHdvIHdlcmUgdGhlCiAgICAgICAgI3N0cmFnZ2xlcnMuCiAgICAgICAgY29uLnNlcnZl"
    "ci5kaXN0LmFkZCh7CiAgICAgICAgICAgICd0YXJnZXQnOmxpc3Qoc2VsZi5jaGF0Q2hhbm5lbHNb"
    "bmFtXSksCiAgICAgICAgICAgICdtZXNzYWdlJzpjb24udXNlci5nZXRDQ1Vtc2coKX0pCiAgICAg"
    "ICAgc2VsZi5jaGF0Q2hhbm5lbHNbbmFtXS5hcHBlbmQoY29uKQogICAgICAgIGNvbi51c2VyLmNo"
    "YXRjaGFubmVsID0gc2VsZi5jaGF0Q2hhbm5lbHNbbmFtXQogICAgICAgIHVsID0gMSNsZW4oY29u"
    "LnVzZXIuY2hhdGNoYW5uZWwpCiAgICAgICAgcmV0bXNnID0gX2VtKGYnL2pvaW5jaGF0Y2hhbm5l"
    "bCAie25hbX0iICIiICJ7dWx9IicpCiAgICAgICAgI2VudW1lcmF0ZSBvdGhlciBjaGF0IHVzZXJz"
    "PwogICAgICAgIGNodW5rcyA9IFtdCiAgICAgICAgZm9yIHVjb24gaW4gbGlzdChjb24udXNlci5j"
    "aGF0Y2hhbm5lbCk6CiAgICAgICAgICAgIGlmIHVjb24gIT0gY29uOgogICAgICAgICAgICAgICAg"
    "Y2h1bmtzLmFwcGVuZCh1Y29uLnVzZXIuZ2V0Q0NVbXNnKCkpCiAgICAgICAgcmV0bXNnKz1iJycu"
    "am9pbihjaHVua3MpCiAgICAgICAgcmV0dXJuIHJldG1zZwogICAgZGVmIGVudW1DaGF0cyhzZWxm"
    "KToKICAgICAgICBjaHVua3MgPSBbXQogICAgICAgIGZvciBjaGF0TmFtZSBpbiBsaXN0KHNlbGYu"
    "Y2hhdENoYW5uZWxzKToKICAgICAgICAgICAgdWxsID0gbGVuKHNlbGYuY2hhdENoYW5uZWxzW2No"
    "YXROYW1lXSkjVE9ETyBpbXByb3ZlCiAgICAgICAgICAgIGNodW5rcy5hcHBlbmQod2lyZV9lbmNv"
    "ZGUoZickY2hhdGNoYW5uZWwgIntjaGF0TmFtZX0iICIiICJ7dWxsfSInKSkKICAgICAgICBpZiBu"
    "b3QgY2h1bmtzOgogICAgICAgICAgICByZXR1cm4gYicnICNuZXZlciBhIGxvbmUgdGVybWluYXRv"
    "cjogdGhhdCBpcyBhbiBlbXB0eSBjb21tYW5kIGxpbmUKICAgICAgICByZXR1cm4gX04uam9pbihj"
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
    "ICAgICAgY3VyID0gW10KICAgICAgICBwcmVmaXggPSBsZW4oJy91cGRoZXJvcG9zICcpCiAgICAg"
    "ICAgY3VybGVuID0gcHJlZml4ICN0aGUgY29tbWFuZCB3b3JkIGNvdW50cyB0b3dhcmRzIHRoZSBs"
    "aW5lLCBpdCB3YXMgbm90CiAgICAgICAgICAgICAgICAgICAgICAgICNiZWluZyBjb3VudGVkLCBz"
    "byBhIGZ1bGwgYmF0Y2ggb3ZlcnNob3QgdGhlIGNhcCBieSAxMgogICAgICAgIGZvciBjaCBpbiBj"
    "aHVua3M6CiAgICAgICAgICAgIGlmIGN1ciBhbmQgY3VybGVuICsgbGVuKGNoKSArIDEgPiBfTUFY"
    "X1dJUkVfTElORToKICAgICAgICAgICAgICAgIGJhdGNoZXMuYXBwZW5kKGN1cikKICAgICAgICAg"
    "ICAgICAgIGN1ciA9IFtdCiAgICAgICAgICAgICAgICBjdXJsZW4gPSBwcmVmaXgKICAgICAgICAg"
    "ICAgY3VyLmFwcGVuZChjaCkKICAgICAgICAgICAgY3VybGVuICs9IGxlbihjaCkgKyAxCiAgICAg"
    "ICAgaWYgY3VyOgogICAgICAgICAgICBiYXRjaGVzLmFwcGVuZChjdXIpCiAgICAgICAgcmV0dXJu"
    "IFtfZW0oJy91cGRoZXJvcG9zICcgKyAnICcuam9pbihiKSkgZm9yIGIgaW4gYmF0Y2hlc10KICAg"
    "IGRlZiBkZWJ1Z19hcnJfZ2FtZXMoc2VsZik6CiAgICAgICAgYWN0RGljdCA9IFtdCiAgICAgICAg"
    "Zm9yIGduLCBnIGluIGxpc3Qoc2VsZi5nYW1lcy5pdGVtcygpKToKICAgICAgICAgICAgYWN0RGlj"
    "dC5hcHBlbmQoZy5kZWJ1Z19kaWN0KCkpCiAgICAgICAgcmV0dXJuIGFjdERpY3QKICAgIGRlZiBk"
    "ZWJ1Z19kaWN0KHNlbGYpOgogICAgICAgIHJldHVybiB7CiAgICAgICAgICAgICd1c2Vycyc6dHVw"
    "bGUoW2MudXNlci5uYW1lIGZvciBjIGluIHNlbGYudXNlcmxpc3RdKSwKICAgICAgICAgICAgJ21h"
    "eFVzZXJzJzpzZWxmLm1heHVzZXIsCiAgICAgICAgICAgICdnYW1lcyc6dHVwbGUoW2duIGZvciBn"
    "biBpbiBzZWxmLmdhbWVzXSkKICAgICAgICB9CgpfTUFQTkFNRVMgPSBbJ05ldF9UXzAxJywnTmV0"
    "X1RfMDInLCdOZXRfVF8wMycsJ05ldF9UXzA0J10gI1RPRE8gdXNlIENGRyBvYmplY3QKY2xhc3Mg"
    "R2FtZVN0YXRlKCk6CiAgICAjVE9ETyBhdXRvIGdyb3dhYmxlIGNoYW5uZWxzLCBbbWFwbmFtZV0K"
    "ICAgICNUT0RPIGF2YWlsYWJsZSBpbmRleGVzLCBbbWFwbmFtZV0KICAgIGRlZiBfX2luaXRfXyhz"
    "ZWxmLCBzZXJ2ZXIpOgogICAgICAgICNpbnN0YW5jZSBhdHRyaWJ1dGVzLCBub3QgY2xhc3MgYXR0"
    "cmlidXRlczogdGhlc2UgbXVzdCBOT1QgYmUgc2hhcmVkCiAgICAgICAgI2JldHdlZW4gc2VwYXJh"
    "dGUgQ29yZVNlcnZlciBpbnN0YW5jZXMgKGUuZy4gc3RvcC9zdGFydCBmcm9tIGEgR1VJCiAgICAg"
    "ICAgI3dpdGhpbiB0aGUgc2FtZSBwcm9jZXNzKSBvciBsZWZ0b3ZlciBwbGF5ZXJzL2NoYW5uZWxz"
    "IGZyb20gYQogICAgICAgICNwcmV2aW91cyBydW4gd291bGQgbGVhayBpbnRvIHRoZSBuZXcgb25l"
    "LgogICAgICAgIHNlbGYuYWN0aXZlVXNlcnMgPSB7fSAjVE9ETyB0cmFjayB1c2VyIGhpc3Rvcnk/"
    "IG9wdGlvbmFsbHkKICAgICAgICBzZWxmLmdhbWVDaGFubmVscyA9IHt9ICNjaGFubmVsW10sIGtl"
    "eWVkIGJ5IG1hcG5hbWUKICAgICAgICBzZWxmLnNlcnZlcj1zZXJ2ZXIKICAgICAgICBzZWxmLnVz"
    "ZXJMb2NrID0gdGhyZWFkaW5nLkxvY2soKQogICAgICAgIGZvciBuYW1lIGluIF9NQVBOQU1FUzoK"
    "ICAgICAgICAgICAgZm9yIGkgaW4gcmFuZ2UoMSk6ICNUT0RPIGNvbmZpZ3VyZWFibGUgdXAgdG8g"
    "MjA/CiAgICAgICAgICAgICAgICBjaG5OYW1lID0gX2djaG5sKG5hbWUsIDEraSkKICAgICAgICAg"
    "ICAgICAgIHNlbGYuZ2FtZUNoYW5uZWxzW2Nobk5hbWVdID0gR2FtZUNoYW5uZWwoc2VsZi5zZXJ2"
    "ZXIsIGNobk5hbWUpICNUT0RPIDEgYW5kIGdyb3c/CiAgICBkZWYgY2xhaW1Vc2VyKHNlbGYsIG5h"
    "bWUsIGNvbik6CiAgICAgICAgI1B1Ymxpc2ggY29uIGFzIFRIRSBsaXZlIHNlc3Npb24gZm9yIG5h"
    "bWUsIGF0b21pY2FsbHkuIFRoZSBvbGQgY29kZQogICAgICAgICNjaGVja2VkIGdldFBsYXllcigp"
    "IGR1cmluZyBsb2dpbiBhbmQgdGhlbiBpbnNlcnRlZCBpbnRvIGFjdGl2ZVVzZXJzCiAgICAgICAg"
    "I211Y2ggbGF0ZXIsIGluIF9sb2JieUhhbmRsZTsgdHdvIGNvbm5lY3Rpb25zIGxvZ2dpbmcgaW4g"
    "YXMgdGhlIHNhbWUKICAgICAgICAjYWNjb3VudCBhdCBvbmNlIGJvdGggcGFzc2VkIHRoZSBjaGVj"
    "aywgYW5kIHRoZSBzZWNvbmQgb25lJ3MgaW5zZXJ0CiAgICAgICAgI292ZXJ3cm90ZSB0aGUgZmly"
    "c3QuIFRoZSBsb3NlciB0aGVuIGRlbGV0ZWQgdGhlIHdpbm5lcidzIGVudHJ5IHdoZW4gaXQKICAg"
    "ICAgICAjZGlzY29ubmVjdGVkLCBsZWF2aW5nIGEgY29ubmVjdGVkIHBsYXllciBpbnZpc2libGUg"
    "dG8gdGhlIHNlcnZlciAobm8KICAgICAgICAja2ljaywgbm8gd2hvaXMsIG5vIG1lc3NhZ2VzKS4K"
    "ICAgICAgICB3aXRoIHNlbGYudXNlckxvY2s6CiAgICAgICAgICAgIGlmIG5hbWUgaW4gc2VsZi5h"
    "Y3RpdmVVc2VyczoKICAgICAgICAgICAgICAgIHJldHVybiBGYWxzZQogICAgICAgICAgICBzZWxm"
    "LmFjdGl2ZVVzZXJzW25hbWVdID0gY29uCiAgICAgICAgICAgIHJldHVybiBUcnVlCiAgICBkZWYg"
    "cmVsZWFzZVVzZXIoc2VsZiwgbmFtZSwgY29uKToKICAgICAgICAjb25seSBjbGVhciB0aGUgc2xv"
    "dCBpZiB3ZSBzdGlsbCBvd24gaXQsIG5ldmVyIHNvbWVvbmUgZWxzZSdzIHNlc3Npb24KICAgICAg"
    "ICB3aXRoIHNlbGYudXNlckxvY2s6CiAgICAgICAgICAgIGlmIHNlbGYuYWN0aXZlVXNlcnMuZ2V0"
    "KG5hbWUpIGlzIGNvbjoKICAgICAgICAgICAgICAgIGRlbCBzZWxmLmFjdGl2ZVVzZXJzW25hbWVd"
    "CiAgICBkZWYgZW51bWVyYXRlR0Moc2VsZik6CiAgICAgICAgY2hucyA9IFtdCiAgICAgICAgZm9y"
    "IGNobk5hbWUgaW4gbGlzdChzZWxmLmdhbWVDaGFubmVscyk6CiAgICAgICAgICAgIGNobiA9IHNl"
    "bGYuZ2FtZUNoYW5uZWxzW2Nobk5hbWVdCiAgICAgICAgICAgIGNobnMuYXBwZW5kKHdpcmVfZW5j"
    "b2RlKGYnJGdhbWVjaGFubmVsICJ7Y2huTmFtZX0iICJ7bGVuKGNobi51c2VybGlzdCl9IiAie2No"
    "bi5tYXh1c2VyfSIgIjAiICIwIicpKSAjVE9ETyBBdmFpbGFibGUgLSBBbGwKICAgICAgICBpZiBu"
    "b3QgY2huczoKICAgICAgICAgICAgcmV0dXJuIGInJyAjc2VlIGVudW1DaGF0cwogICAgICAgIHJl"
    "dHVybiBfTi5qb2luKGNobnMpK19OCiAgICBkZWYgdXBkYXRlUG9zKHNlbGYpOgogICAgICAgIG1k"
    "ID0gc2VsZi5zZXJ2ZXIuZGlzdAogICAgICAgIGZvciBjaG4gaW4gbGlzdChzZWxmLmdhbWVDaGFu"
    "bmVscy52YWx1ZXMoKSk6CiAgICAgICAgICAgIGNobi51cGRhdGVQb3MobWQpCiNoYW5kbGVzIGlu"
    "dGVyYWN0aW9ucyBiZXR3ZWVuIGFsbCBlbGVtZW50cwpjbGFzcyBDb3JlU2VydmVyKHNvY2tldHNl"
    "cnZlci5UaHJlYWRpbmdUQ1BTZXJ2ZXIpOgogICAgYWxsb3dfcmV1c2VfYWRkcmVzcyA9IFRydWUg"
    "IyBUT0RPIGNoZWNrIGlmIGltcHJvdmVzIHJlc3RhcnQgdGltZXMgd2l0aG91dCBvdGhlciBpc3N1"
    "ZXMKICAgIGRhZW1vbl90aHJlYWRzID0gVHJ1ZQogICAgYmxvY2tfb25fY2xvc2UgPSBGYWxzZQog"
    "ICAgX2lzX2Nsb3NpbmcgPSBGYWxzZQogICAgZGVmIF9faW5pdF9fKHNlbGYpOgogICAgICAgICNU"
    "T0RPIGdldCB2YWx1ZXMgZnJvbSBjZmcKICAgICAgICAjYWRkcmVzcyA9ICdsb2NhbGhvc3QnCiAg"
    "ICAgICAgYWRkcmVzcyA9ICcnCiAgICAgICAgcG9ydCA9IF9UV19MT0JCWV9QT1JUCiAgICAgICAg"
    "cHJpbnQoZidJbml0aWFsaXppbmcgc2VydmVyIGZvciBwb3J0IHtwb3J0fScpCiAgICAgICAgc3Vw"
    "ZXIoKS5fX2luaXRfXygoYWRkcmVzcywgcG9ydCksIENvbm5lY3Rpb25IYW5kbGVyKQogICAgICAg"
    "IHNlbGYuZGlzdCA9IE1lc3NhZ2VEaXN0cmlidXRvcihzZWxmKQogICAgICAgIHNlbGYuY29tcGFy"
    "cyA9IENvbW1hbmRQYXJzZXIoc2VsZi5kaXN0KQogICAgICAgIHNlbGYuc3RhdGUgPSBHYW1lU3Rh"
    "dGUoc2VsZikKICAgICAgICBzZWxmLnN0YXJ0VGltZSA9IGRhdGV0aW1lLmRhdGV0aW1lLm5vdygp"
    "CiAgICAgICAgc2VsZi5zZXJ2aWNlX3RpY2sgPSAwCiAgICAgICAgc2VsZi5fcG9zU3RvcCA9IHRo"
    "cmVhZGluZy5FdmVudCgpCiAgICAgICAgc2VsZi5fcG9zVGhyZWFkID0gTm9uZQogICAgICAgICNF"
    "dmVyeSBsaXZlIGNvbm5lY3Rpb24gaGFuZGxlci4gc29ja2V0c2VydmVyJ3Mgc2h1dGRvd24oKSBv"
    "bmx5IHN0b3BzCiAgICAgICAgI3RoZSBhY2NlcHQgbG9vcCBhbmQgY2xvc2VzIHRoZSBsaXN0ZW5p"
    "bmcgc29ja2V0IC0gYWxyZWFkeS1lc3RhYmxpc2hlZAogICAgICAgICNjb25uZWN0aW9ucyBrZWVw"
    "IHRoZWlyIChkYWVtb24pIHRocmVhZHMgcnVubmluZywgc3RpbGwgcmVhZGluZywgc3RpbGwKICAg"
    "ICAgICAjbG9nZ2luZywgZm9yIGFzIGxvbmcgYXMgdGhlIGNsaWVudCBzdGF5cyBjb25uZWN0ZWQu"
    "IEZyb20gdGhlIGNvbnRyb2wKICAgICAgICAjcGFuZWwgdGhhdCBsb29rcyBsaWtlIGEgc2VydmVy"
    "IHRoYXQgd2FzIG5ldmVyIHN0b3BwZWQgYXQgYWxsLgogICAgICAgIHNlbGYuX2Nvbm5zID0gc2V0"
    "KCkKICAgICAgICBzZWxmLl9jb25uTG9jayA9IHRocmVhZGluZy5Mb2NrKCkKICAgIGRlZiBzZXJ2"
    "ZXJfYWN0aXZhdGUoc2VsZik6CiAgICAgICAgcHJpbnQoZidTZXJ2ZXIgU3RhcnRpbmcgYXQgUElE"
    "OiB7b3MuZ2V0cGlkKCl9JykjTE9HCiAgICAgICAgc3VwZXIoKS5zZXJ2ZXJfYWN0aXZhdGUoKQog"
    "ICAgZGVmIGRlYnVnX2RpY3RfcGxheWVycyhzZWxmKToKICAgICAgICAjc25hcHNob3QgdmlhIGxp"
    "c3QoKSBmaXJzdDogaXRlcmF0aW5nIHRoZSBsaXZlIGRpY3QgZGlyZWN0bHkgcmlza3MKICAgICAg"
    "ICAjJ2RpY3Rpb25hcnkgY2hhbmdlZCBzaXplIGR1cmluZyBpdGVyYXRpb24nIHdoZW4gYSBwbGF5"
    "ZXIgY29ubmVjdHMKICAgICAgICAjb3IgZGlzY29ubmVjdHMgd2hpbGUgYSBtb25pdG9yaW5nIFVJ"
    "IGlzIHBvbGxpbmcgdGhpcwogICAgICAgIHJldCA9IHt9CiAgICAgICAgZm9yIG5hbWUsIGNvbiBp"
    "biBsaXN0KHNlbGYuc3RhdGUuYWN0aXZlVXNlcnMuaXRlbXMoKSk6CiAgICAgICAgICAgIHJldFtu"
    "YW1lXSA9IGNvbi5kZWJ1Z19kaWN0KCkKICAgICAgICByZXR1cm4gcmV0CiAgICBkZWYgZGVidWdf"
    "ZGljdF90b3ducyhzZWxmKToKICAgICAgICByZXQgPSB7fQogICAgICAgIGZvciBuYW1lLCBjaG4g"
    "aW4gbGlzdChzZWxmLnN0YXRlLmdhbWVDaGFubmVscy5pdGVtcygpKToKICAgICAgICAgICAgcmV0"
    "W25hbWVdID0gY2huLmRlYnVnX2RpY3QoKQogICAgICAgIHJldHVybiByZXQKICAgIGRlZiBkZWJ1"
    "Z19hcnJfZ2FtZXMoc2VsZik6CiAgICAgICAgcmV0ID0gW10KICAgICAgICBmb3IgbmFtZSwgY2hu"
    "IGluIGxpc3Qoc2VsZi5zdGF0ZS5nYW1lQ2hhbm5lbHMuaXRlbXMoKSk6CiAgICAgICAgICAgICBy"
    "ZXQuZXh0ZW5kKGNobi5kZWJ1Z19hcnJfZ2FtZXMoKSkKICAgICAgICByZXR1cm4gcmV0CiAgICBk"
    "ZWYgX3Bvc0xvb3Aoc2VsZik6CiAgICAgICAgI1Bvc2l0aW9uIGZhbi1vdXQgdXNlZCB0byByaWRl"
    "IG9uIHNlcnZpY2VfYWN0aW9ucygpLCB3aGljaCBzb2NrZXRzZXJ2ZXIKICAgICAgICAjY2FsbHMg"
    "b25jZSBwZXIgcG9sbF9pbnRlcnZhbCAtIG9uZSBzZWNvbmQuIFRoYXQgd2FzIHRoZSBjYWRlbmNl"
    "IGF0CiAgICAgICAgI3doaWNoIG90aGVyIHBsYXllcnMnIG1hcmtlcnMgbW92ZWQgb24gdGhlIG1h"
    "cDogYSBmdWxsIHNlY29uZCBvZiBkZWFkCiAgICAgICAgI3JlY2tvbmluZyBiZXR3ZWVuIHVwZGF0"
    "ZXMsIHdoaWNoIHJlYWRzIGFzIHRlbGVwb3J0aW5nIHJhdGhlciB0aGFuCiAgICAgICAgI3dhbGtp"
    "bmcuIEl0cyBvd24gdGhyZWFkIGRlY291cGxlcyB0aGUgYnJvYWRjYXN0IHJhdGUgZnJvbSB0aGUg"
    "YWNjZXB0CiAgICAgICAgI2xvb3AncyBwb2xsIHJhdGUgc28gaXQgY2FuIHJ1biBzZXZlcmFsIHRp"
    "bWVzIGEgc2Vjb25kLgogICAgICAgIHdoaWxlIG5vdCBzZWxmLl9wb3NTdG9wLmlzX3NldCgpOgog"
    "ICAgICAgICAgICBwZXJpb2QgPSAxLjAgLyBfUE9TX1VQREFURV9IWiBpZiBfUE9TX1VQREFURV9I"
    "WiA+IDAgZWxzZSAxLjAKICAgICAgICAgICAgI3dhaXQoKSByYXRoZXIgdGhhbiBzbGVlcCgpOiBz"
    "aHV0ZG93biBpcyBpbW1lZGlhdGUsIGFuZCByZS1yZWFkaW5nCiAgICAgICAgICAgICN0aGUgcGVy"
    "aW9kIGVhY2ggcGFzcyBtZWFucyBhIGNvbmZpZyBjaGFuZ2UgdGFrZXMgZWZmZWN0IGxpdmUuCiAg"
    "ICAgICAgICAgIGlmIHNlbGYuX3Bvc1N0b3Aud2FpdChwZXJpb2QpOgogICAgICAgICAgICAgICAg"
    "YnJlYWsKICAgICAgICAgICAgdHJ5OgogICAgICAgICAgICAgICAgc2VsZi5zdGF0ZS51cGRhdGVQ"
    "b3MoKQogICAgICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICAgICAgI25ldmVy"
    "IGxldCBvbmUgYmFkIGNoYW5uZWwga2lsbCBwb3NpdGlvbiBzeW5jIGZvciBldmVyeW9uZQogICAg"
    "ICAgICAgICAgICAgcHJpbnQoJ1tMb2JieV0gUG9zaXRpb24gdXBkYXRlIGVycm9yOlxuJyArIHRy"
    "YWNlYmFjay5mb3JtYXRfZXhjKCkpCiAgICBkZWYgc2VydmljZV9hY3Rpb25zKHNlbGYpOiAjY2Fs"
    "bGVkIGV2ZXJ5IHBvbGxfaW50ZXJ2YWwKICAgICAgICAjIHRpbWUgaW50ZXJ2YWxzCiAgICAgICAg"
    "I1JlYWQgbGl2ZSwgbm90IGZyb20gdGhlIGNvcHkgdGFrZW4gd2hlbiB0aGlzIHNlcnZlciBvYmpl"
    "Y3Qgd2FzIGJ1aWx0LgogICAgICAgICNFdmVyeSBvdGhlciBzeW5jaHJvbmlzYXRpb24gc2V0dGlu"
    "ZyB0YWtlcyBlZmZlY3Qgb24gYSBydW5uaW5nIHNlcnZlciAtCiAgICAgICAgI2FwcGx5Q29uZmln"
    "KCkgd3JpdGVzIHRoZSBtb2R1bGUgZ2xvYmFscyBhbmQgdGhlIGxvb3BzIHJlLXJlYWQgdGhlbSAt"
    "CiAgICAgICAgI3doaWNoIG1hZGUgdGhpcyB0aGUgb25lIHN3aXRjaCBpbiB0aGF0IGdyb3VwIHRo"
    "YXQgc2lsZW50bHkgZGlkIG5vdGhpbmcKICAgICAgICAjdW50aWwgdGhlIG5leHQgcmVzdGFydCwg"
    "d2hpbGUgdGhlIEdVSSBzYWlkIG90aGVyd2lzZS4KICAgICAgICBpZiBfU0VORF9OT1BTIGFuZCAo"
    "c2VsZi5zZXJ2aWNlX3RpY2slMyk9PTA6CiAgICAgICAgICAgIHNlbGYuZGlzdC5hZGQoeyd0YXJn"
    "ZXQnOnNlbGYuc3RhdGUuYWN0aXZlVXNlcnMudmFsdWVzKCksJ21lc3NhZ2UnOl9lbSgnL25vcCcp"
    "fSkKICAgICAgICAgICAgI3NlbmQgJy9ub3AnIHRvIGFsbCBldmVyeSAzIHNlYyBvcHRpb25hbGx5"
    "CiAgICAgICAgI3NlcnZpY2UgdGljayAzIGRheSByZXNldCBpbnRlcnZhbCBUT0RPIHRlc3QgYWxp"
    "Z25tZW50IHdpdGggb3RoZXIgZmFjdG9ycwogICAgICAgIHNlbGYuc2VydmljZV90aWNrID0gKHNl"
    "bGYuc2VydmljZV90aWNrKzEpJSg2MCo2MCoyNCozKQogICAgICAgIHN1cGVyKCkuc2VydmljZV9h"
    "Y3Rpb25zKCkKICAgIGRlZiBzZXJ2ZV9mb3JldmVyKHNlbGYpOgogICAgICAgIGRpc3RUaHJlYWQg"
    "PSB0aHJlYWRpbmcuVGhyZWFkKHRhcmdldD1zZWxmLmRpc3Quc2VydmVfZm9yZXZlcikKICAgICAg"
    "ICBkaXN0VGhyZWFkLnN0YXJ0KCkKICAgICAgICBzZWxmLl9wb3NTdG9wLmNsZWFyKCkKICAgICAg"
    "ICBzZWxmLl9wb3NUaHJlYWQgPSB0aHJlYWRpbmcuVGhyZWFkKHRhcmdldD1zZWxmLl9wb3NMb29w"
    "LCBkYWVtb249VHJ1ZSkKICAgICAgICBzZWxmLl9wb3NUaHJlYWQuc3RhcnQoKQogICAgICAgICNw"
    "b2xsX2ludGVydmFsIGlzIG5vdyBvbmx5IHRoZSBhY2NlcHQgbG9vcCdzIHNodXRkb3duIHJlc3Bv"
    "bnNpdmVuZXNzIC0KICAgICAgICAjcG9zaXRpb24gYnJvYWRjYXN0cyBubyBsb25nZXIgcmlkZSBv"
    "biBpdAogICAgICAgIHN1cGVyKCkuc2VydmVfZm9yZXZlcigxKQogICAgICAgIHNlbGYuX3Bvc1N0"
    "b3Auc2V0KCkKICAgICAgICBpZiBzZWxmLl9wb3NUaHJlYWQ6CiAgICAgICAgICAgIHNlbGYuX3Bv"
    "c1RocmVhZC5qb2luKHRpbWVvdXQ9Mi4wKQogICAgICAgICAgICBzZWxmLl9wb3NUaHJlYWQgPSBO"
    "b25lCiAgICAgICAgc2VsZi5kaXN0LmVuZCgpI2luIGNhc2UgaXQgaGFzbid0IGFscmVhZHkKICAg"
    "ICAgICBkaXN0VGhyZWFkLmpvaW4oKQogICAgZGVmIGhhbmRsZV9zaWduYWwoc2VsZiwgdGltZW91"
    "dCk6CiAgICAgICAgZGVmIGhhbmRsZXIoc2lnbnVtLCBfKToKICAgICAgICAgICAgZGVhZGxpbmUg"
    "PSB0aW1lLm1vbm90b25pYygpICsgdGltZW91dAogICAgICAgICAgICBzaWduYW1lID0gc2lnbmFs"
    "LlNpZ25hbHMoc2lnbnVtKS5uYW1lCiAgICAgICAgICAgIHNlbGYuX2lzX2Nsb3NpbmcgPSBUcnVl"
    "ICNUT0RPIHByb3Blcmx5IGVuZCBjb25uZWN0aW9ucyBhZnRlciBhIGRlbGF5CiAgICAgICAgICAg"
    "IHByaW50KGYnQ2xvc2luZyBpbiB7dGltZW91dH0nKQogICAgICAgICAgICAjd2hpbGUgKGN1cnJl"
    "bnRfdGltZSA6PSB0aW1lLm1vbm90b25pYygpKSA8IGRlYWRsaW5lOgogICAgICAgICAgICAjICAg"
    "IGRlbHRhID0gaW50KGRlYWRsaW5lIC0gY3VycmVudF90aW1lKQogICAgICAgICAgICAgICAgI1RP"
    "RE8gc2lnbmFsIHRvIHBsYXllcnMgdGhhdCBjb25uZWN0aW9uIGlzIHNodXR0aW5nIGRvd24KICAg"
    "ICAgICAgICAgICAgICMtIHNlbGYuc3RhdGUuYWN0aXZlVXNlcnMudmFsdWVzKCkKICAgICAgICAg"
    "ICAgICAgICMtIGYnL2FkbWluIFNlcnZlciBjbG9zaW5nIGluIHtkZWx0YX0nLmVuY29kZSgnYXNj"
    "aWknKStfTgogICAgICAgICAgICAgICAgI0xPRyBDTE9TRQogICAgICAgICAgICAgICAgI1RPRE8g"
    "YmV0dGVyIHNodXRkb3duIGhhbmRsaW5nCiAgICAgICAgICAgICMgICAgdGltZS5zbGVlcCgxKQog"
    "ICAgICAgICAgICB0aW1lLnNsZWVwKHRpbWVvdXQpI2FsdCB3aGlsZSBvdGhlciBzdHVmZiBpcyBv"
    "bmdvaW5nCiAgICAgICAgICAgIHNlbGYuX0Jhc2VTZXJ2ZXJfX3NodXRkb3duX3JlcXVlc3QgPSBU"
    "cnVlCiAgICAgICAgICAgICNzZWxmLnNodXRkb3duKCkgI29ubHkgaWYgc2VydmVfZm9yZXZlciBp"
    "cyBpbiBhIGRpZmZlcmVudCB0aHJlYWQKICAgICAgICAgICAgI3NlbGYuc2VydmVyX2Nsb3NlKCkg"
    "I29ubHkgbmVlZGVkIGlmIG5vdCB1c2luZyBhIHdpdGggc3RhdGVtZW50CiAgICAgICAgcmV0dXJu"
    "IGhhbmRsZXIKICAgIGRlZiByZWdpc3RlckNvbm5lY3Rpb24oc2VsZiwgY29uKToKICAgICAgICB3"
    "aXRoIHNlbGYuX2Nvbm5Mb2NrOgogICAgICAgICAgICBzZWxmLl9jb25ucy5hZGQoY29uKQogICAg"
    "ZGVmIHVucmVnaXN0ZXJDb25uZWN0aW9uKHNlbGYsIGNvbik6CiAgICAgICAgd2l0aCBzZWxmLl9j"
    "b25uTG9jazoKICAgICAgICAgICAgc2VsZi5fY29ubnMuZGlzY2FyZChjb24pCiAgICBkZWYgY2xv"
    "c2VDb25uZWN0aW9ucyhzZWxmKToKICAgICAgICAjRHJvcCBldmVyeSBjbGllbnQuIFNodXR0aW5n"
    "IHRoZSBzb2NrZXQgZG93biB1bmJsb2NrcyB3aGljaGV2ZXIKICAgICAgICAjc2VsZWN0KCkvcmVj"
    "digpIHRoYXQgY29ubmVjdGlvbidzIHRocmVhZCBpcyBzaXR0aW5nIGluLCBzbyBpdCBydW5zCiAg"
    "ICAgICAgI2l0cyBub3JtYWwgY2xlYW51cCBwYXRoIGFuZCBleGl0cyBpbnN0ZWFkIG9mIGxpbmdl"
    "cmluZy4KICAgICAgICB3aXRoIHNlbGYuX2Nvbm5Mb2NrOgogICAgICAgICAgICBjb25ucyA9IGxp"
    "c3Qoc2VsZi5fY29ubnMpCiAgICAgICAgZm9yIGNvbiBpbiBjb25uczoKICAgICAgICAgICAgY29u"
    "LmRyb3AoKQogICAgICAgIHJldHVybiBsZW4oY29ubnMpCiAgICBkZWYgc2h1dGRvd24oc2VsZik6"
    "CiAgICAgICAgI1N0b3BwaW5nIHRoZSBzZXJ2ZXIgbWVhbnMgc3RvcHBpbmcgaXQ6IGZsYWcgaXQg"
    "Zmlyc3Qgc28gdGhlIHJlYWQKICAgICAgICAjbG9vcHMgYmFpbCBvdXQgcmF0aGVyIHRoYW4gc2Vy"
    "dmluZyBhbm90aGVyIGNvbW1hbmQsIHRoZW4gc3RvcCB0aGUKICAgICAgICAjYWNjZXB0IGxvb3As"
    "IHRoZW4gZXZpY3QgZXZlcnlvbmUgc3RpbGwgY29ubmVjdGVkLgogICAgICAgIHNlbGYuX2lzX2Ns"
    "b3NpbmcgPSBUcnVlCiAgICAgICAgc3VwZXIoKS5zaHV0ZG93bigpCiAgICAgICAgbiA9IHNlbGYu"
    "Y2xvc2VDb25uZWN0aW9ucygpCiAgICAgICAgaWYgbjoKICAgICAgICAgICAgcHJpbnQoZidbTG9i"
    "YnldIENsb3NlZCB7bn0gY2xpZW50IGNvbm5lY3Rpb24ocykgb24gc2h1dGRvd24nKQogICAgZGVm"
    "IGdldFBsYXllcihzZWxmLCB1c2VybmFtZSk6CiAgICAgICAgcmV0dXJuIHNlbGYuc3RhdGUuYWN0"
    "aXZlVXNlcnMuZ2V0KHVzZXJuYW1lKQogICAgZGVmIGtpY2tQbGF5ZXIoc2VsZiwgdXNlcm5hbWUs"
    "IHJlYXNvbj0nS2lja2VkIGJ5IGFkbWluJyk6CiAgICAgICAgI0FkbWluLXBhbmVsIGFjdGlvbjog"
    "Zm9yY2libHkgZGlzY29ubmVjdCBhIGNvbm5lY3RlZCBwbGF5ZXIuIFNlbmRzIGEKICAgICAgICAj"
    "YmVzdC1lZmZvcnQgL2FkbWluIG5vdGljZSBmaXJzdCAoY2xpZW50IHNob3dzIGl0IGxpa2UgYW55"
    "IG90aGVyCiAgICAgICAgI3NlcnZlciBhZG1pbiBtZXNzYWdlKSwgdGhlbiBzaHV0cyBkb3duIHRo"
    "ZSBzb2NrZXQgc28gdGhlIHBsYXllcidzCiAgICAgICAgI2hhbmRsZXIgdGhyZWFkIHVuYmxvY2tz"
    "IGZyb20gaXRzIHJlY3YoKSBhbmQgcnVucyBpdHMgbm9ybWFsCiAgICAgICAgI2Rpc2Nvbm5lY3Qv"
    "Y2xlYW51cCBwYXRoLgogICAgICAgIGNvbiA9IHNlbGYuZ2V0UGxheWVyKHVzZXJuYW1lKQogICAg"
    "ICAgIGlmIGNvbiBpcyBOb25lOgogICAgICAgICAgICByZXR1cm4gRmFsc2UKICAgICAgICAjUXVl"
    "dWVkLCBub3Qgd3JpdHRlbiBpbmxpbmUuIHNlbmRSYXcoKSB0YWtlcyB0aGF0IGNvbm5lY3Rpb24n"
    "cyBzZW5kCiAgICAgICAgI2xvY2ssIGFuZCBpdHMgd3JpdGVyIHRocmVhZCBob2xkcyB0aGF0IGxv"
    "Y2sgZm9yIHRoZSB3aG9sZSBvZiBhCiAgICAgICAgI2Jsb2NraW5nIHNlbmRhbGwoKSAtIHNvIGtp"
    "Y2tpbmcgYSBwbGF5ZXIgd2hvc2UgbGluayBoYWQgc3RhbGxlZCBibG9ja2VkCiAgICAgICAgI3do"
    "b2V2ZXIgY2FsbGVkIHRoaXMgdW50aWwgdGhlIHN0YWxsZWQgY2xpZW50IHdlbnQgYXdheSwgYW5k"
    "IHRoZSBjYWxsZXIKICAgICAgICAjaGVyZSBpcyB0aGUgR1VJIHRocmVhZC4gVGhlIGFkbWluIHBh"
    "bmVsIGZyb3plIG9uIGV4YWN0bHkgdGhlIHBsYXllciBpdAogICAgICAgICN3YXMgdHJ5aW5nIHRv"
    "IGdldCByaWQgb2YuIEEgcXVldWUgcHV0IGNhbm5vdCBibG9jay4KICAgICAgICB0cnk6CiAgICAg"
    "ICAgICAgIGNvbi5zZW5kKF9lbShmJy9hZG1pbiB7cmVhc29ufScpKQogICAgICAgIGV4Y2VwdCBF"
    "eGNlcHRpb246CiAgICAgICAgICAgIHBhc3MgI2Jlc3QgZWZmb3J0LCBjb25uZWN0aW9uIG1heSBh"
    "bHJlYWR5IGJlIG9uIGl0cyB3YXkgb3V0CiAgICAgICAgY29uLmZsdXNoUGVuZGluZygwLjMpICNi"
    "b3VuZGVkOiBnaXZlIHRoZSBub3RpY2UgYSBjaGFuY2UgdG8gZ28gb3V0CiAgICAgICAgY29uLmRy"
    "b3AoKQogICAgICAgIHJldHVybiBUcnVlCiAgICBkZWYgZGVsZXRlQWNjb3VudChzZWxmLCB1c2Vy"
    "bmFtZSk6CiAgICAgICAgI0FkbWluLXBhbmVsIGFjdGlvbjogcGVybWFuZW50bHkgZGVsZXRlcyBh"
    "IGNoYXJhY3Rlci9hY2NvdW50LgogICAgICAgICNLaWNrcyBmaXJzdCAobm8tb3AgaWYgYWxyZWFk"
    "eSBvZmZsaW5lKSBzbyBhIGNvbm5lY3RlZCBjbGllbnQgbmV2ZXIKICAgICAgICAja2VlcHMgcGxh"
    "eWluZyBvbiBhbiBhY2NvdW50IHRoYXQgaGFzIGp1c3QgdmFuaXNoZWQgZnJvbSB0aGUgREIuCiAg"
    "ICAgICAgc2VsZi5raWNrUGxheWVyKHVzZXJuYW1lLCByZWFzb249J0FjY291bnQgZGVsZXRlZCBi"
    "eSBhZG1pbicpCiAgICAgICAgcmV0dXJuIEdESC5kZWxldGVBY2NvdW50KHVzZXJuYW1lKQojRmFp"
    "bGVkLWxvZ2luIHRocm90dGxlLCBwZXIgc291cmNlIElQLgojVHdvIHJlYXNvbnMgdGhpcyBpcyBu"
    "b3Qgb3B0aW9uYWwgb24gYSBzZXJ2ZXIgcmVhY2hhYmxlIGZyb20gdGhlIGludGVybmV0OgojYSBw"
    "YXNzd29yZCBndWVzcyBpcyBjaGVhcCBmb3IgdGhlIGF0dGFja2VyIGJ1dCBjb3N0cyAqdXMqIGEg"
    "MTAway1pdGVyYXRpb24KI1BCS0RGMiAodGVucyBvZiBtcyBvZiBDUFUgZWFjaCksIHNvIGFuIHVu"
    "dGhyb3R0bGVkIGxvZ2luIGVuZHBvaW50IGlzIGJvdGggYQojYnJ1dGUtZm9yY2Ugb3JhY2xlIGFu"
    "ZCBhIENQVSBhbXBsaWZpZXIgLSBhIGhhbmRmdWwgb2YgY29ubmVjdGlvbnMgY2FuIHBpbgojZXZl"
    "cnkgY29yZS4gU3VjY2Vzc2Z1bCBsb2dpbnMgY2xlYXIgdGhlIGNvdW50ZXIsIHNvIGEgcGxheWVy"
    "IGZ1bWJsaW5nIHRoZWlyCiNwYXNzd29yZCBhIGZldyB0aW1lcyBpcyBuZXZlciBsb2NrZWQgb3V0"
    "IGZvciBsb25nLgpfTE9HSU5fRkFJTF9MSU1JVCA9IDYgICAgICAjZmFpbHVyZXMgYWxsb3dlZCBp"
    "bnNpZGUgdGhlIHdpbmRvdyBiZWZvcmUgZGVsYXlpbmcKX0xPR0lOX0ZBSUxfV0lORE9XID0gMzAw"
    "ICAgI3NlY29uZHMgYSBmYWlsdXJlIGlzIHJlbWVtYmVyZWQKX0xPR0lOX0ZBSUxfREVMQVkgPSAy"
    "LjAgICAgI3NlY29uZHMgdG8gc3RhbGwgZWFjaCBhdHRlbXB0IG9uY2Ugb3ZlciB0aGUgbGltaXQK"
    "Y2xhc3MgTG9naW5UaHJvdHRsZSgpOgogICAgZGVmIF9faW5pdF9fKHNlbGYpOgogICAgICAgIHNl"
    "bGYubG9jayA9IHRocmVhZGluZy5Mb2NrKCkKICAgICAgICBzZWxmLmZhaWxzID0ge30gI2lwIC0+"
    "IFt0aW1lc3RhbXBzXQogICAgZGVmIF9wcnVuZShzZWxmLCBpcCwgbm93KToKICAgICAgICByZWNl"
    "bnQgPSBbdCBmb3IgdCBpbiBzZWxmLmZhaWxzLmdldChpcCwgKCkpIGlmIG5vdyAtIHQgPCBfTE9H"
    "SU5fRkFJTF9XSU5ET1ddCiAgICAgICAgaWYgcmVjZW50OgogICAgICAgICAgICBzZWxmLmZhaWxz"
    "W2lwXSA9IHJlY2VudAogICAgICAgIGVsc2U6CiAgICAgICAgICAgIHNlbGYuZmFpbHMucG9wKGlw"
    "LCBOb25lKQogICAgICAgIHJldHVybiByZWNlbnQKICAgIGRlZiBkZWxheUZvcihzZWxmLCBpcCk6"
    "CiAgICAgICAgbm93ID0gdGltZS5tb25vdG9uaWMoKQogICAgICAgIHdpdGggc2VsZi5sb2NrOgog"
    "ICAgICAgICAgICByZWNlbnQgPSBzZWxmLl9wcnVuZShpcCwgbm93KQogICAgICAgIHJldHVybiBf"
    "TE9HSU5fRkFJTF9ERUxBWSBpZiBsZW4ocmVjZW50KSA+PSBfTE9HSU5fRkFJTF9MSU1JVCBlbHNl"
    "IDAuMAogICAgZGVmIHJlY29yZEZhaWx1cmUoc2VsZiwgaXApOgogICAgICAgIG5vdyA9IHRpbWUu"
    "bW9ub3RvbmljKCkKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgcmVjZW50ID0g"
    "c2VsZi5fcHJ1bmUoaXAsIG5vdykKICAgICAgICAgICAgcmVjZW50LmFwcGVuZChub3cpCiAgICAg"
    "ICAgICAgIHNlbGYuZmFpbHNbaXBdID0gcmVjZW50CiAgICAgICAgICAgIHJldHVybiBsZW4ocmVj"
    "ZW50KQogICAgZGVmIHJlY29yZFN1Y2Nlc3Moc2VsZiwgaXApOgogICAgICAgIHdpdGggc2VsZi5s"
    "b2NrOgogICAgICAgICAgICBzZWxmLmZhaWxzLnBvcChpcCwgTm9uZSkKTE9HSU5fVEhST1RUTEUg"
    "PSBMb2dpblRocm90dGxlKCkKCl9MT0dJTl9FUlJPUlMgPSB7CiAgICAxOiAnSW52YWxpZCB1c2Vy"
    "bmFtZSBvciBwYXNzd29yZCcsCiAgICAyOiAnQWNjb3VudCBhbHJlYWR5IGxvZ2dlZCBpbicsCiAg"
    "ICAzOiAnUGFzc3dvcmQgcmVxdWlyZWQnLAogICAgNDogJ1VzZXJuYW1lIHJlcXVpcmVkJywKICAg"
    "ICNBY2NvdW50cyBhcmUgdGllZCB0byB0aGUgc2VyaWFsIHRoZSBjbGllbnQgaGFuZHNoYWtlcyB3"
    "aXRoLCBzbyBhCiAgICAjcmVpbnN0YWxsZWQgb3IgcmUta2V5ZWQgZ2FtZSBjYW5ub3QgcmVhY2gg"
    "YW4gZXhpc3RpbmcgYWNjb3VudCBubyBtYXR0ZXIKICAgICN3aGF0IHBhc3N3b3JkIGl0IHR5cGVz"
    "LiBTYXkgdGhhdCwgcmF0aGVyIHRoYW4gYmxhbWluZyB0aGUgbmFtZS4KICAgIDU6ICdUaGlzIG5h"
    "bWUgYmVsb25ncyB0byBhbiBhY2NvdW50IHJlZ2lzdGVyZWQgd2l0aCBhIGRpZmZlcmVudCBnYW1l"
    "IHNlcmlhbCcsCn0KX1JFR0lTVEVSX0VSUk9SUyA9IHsKICAgIDE6ICdBY2NvdW50IGFscmVhZHkg"
    "bG9nZ2VkIGluJywKICAgIDI6ICdVc2VybmFtZSB1bmF2YWlsYWJsZSBvciBpbnZhbGlkJywKfQoj"
    "Q2VpbGluZyBvbiBob3cgbXVjaCB1bnNlbnQgZGF0YSBtYXkgcGlsZSB1cCBmb3IgYSBzaW5nbGUg"
    "Y2xpZW50LiBUaGUgd3JpdGVyCiN0aHJlYWQgYmxvY2tzIGluc2lkZSBzZW5kYWxsKCkgZm9yIGV4"
    "YWN0bHkgYXMgbG9uZyBhcyBhIGNsaWVudCByZWZ1c2VzIHRvIHJlYWQsCiNhbmQgYSBmcm96ZW4g"
    "Z2FtZSBkb2VzIHByZWNpc2VseSB0aGF0IC0gd2hpbGUgYWxzbyBzZW5kaW5nIG5vdGhpbmcsIHNv"
    "IG5vdGhpbmcKI2Vsc2Ugbm90aWNlcyBpdCB1bnRpbCBhIGZ1bGwgaWRsZSB0aW1lb3V0IGhhcyBw"
    "YXNzZWQuIEZvciB0aG9zZSBtaW51dGVzIGV2ZXJ5CiNwb3NpdGlvbiBicm9hZGNhc3QsIGV2ZXJ5"
    "IGNoYXQgbGluZSBhbmQgZXZlcnkgcmVsYXllZCBnYW1lIGNvbW1hbmQgZm9yIHRoYXQKI3BsYXll"
    "ciBrZXB0IGJlaW5nIGFwcGVuZGVkIHRvIGFuIHVuYm91bmRlZCBxdWV1ZS4gQm91bmRpbmcgaXQg"
    "dHVybnMgInRoZSBzZXJ2ZXIKI3F1aWV0bHkgZ3Jvd3Mgb24gYmVoYWxmIG9mIGEgY2xpZW50IHRo"
    "YXQgaXMgYWxyZWFkeSBnb25lIiBpbnRvIGEgY2xlYW4gZHJvcAojd2l0aCBhIGxpbmUgaW4gdGhl"
    "IGxvZy4gU2l6ZWQgZmFyIGFib3ZlIGFueSBsZWdpdGltYXRlIGJ1cnN0OiB0aGUgbGFyZ2VzdAoj"
    "c2luZ2xlIHRoaW5nIHRoYXQgZ29lcyBvdXQgaXMgYSBoZXJvZGF0YSBibG9iLCBhbmQgYSB3aG9s"
    "ZSB0b3duIG9mIHRoZW0gZG9lcwojbm90IGNvbWUgY2xvc2UuCl9NQVhfU0VORF9CQUNLTE9HID0g"
    "NCAqIDEwMjQgKiAxMDI0CiNoYW5kbGVzIGluZGl2aWR1YWwgY29ubmVjdGlvbnMKY2xhc3MgQ29u"
    "bmVjdGlvbkhhbmRsZXIoc29ja2V0c2VydmVyLkJhc2VSZXF1ZXN0SGFuZGxlcik6CiAgICAjZGVm"
    "YXVsdCBwcm9wZXJ0aWVzOgogICAgIyAtIHJlcXVlc3Q6IHNvY2tldCB0byBkZXN0aW5hdGlvbgog"
    "ICAgIyAtIGNsaWVudF9hZGRyZXNzCiAgICAjIC0gc2VydmVyOiBDb3JlU2VydmVyCiAgICBfU1RP"
    "UFdSSVRFUiA9IG9iamVjdCgpCiAgICBkZWYgc2V0dXAoc2VsZik6CiAgICAgICAgc2VsZi5fc1F1"
    "ZXVlID0gU2ltcGxlUXVldWUoKQogICAgICAgICNCeXRlcyBxdWV1ZWQgYnV0IG5vdCB5ZXQgaGFu"
    "ZGVkIHRvIHNlbmRhbGwoKSwgYW5kIHRoZSBmbGFnIHRoYXQgc2F5cwogICAgICAgICN0aGlzIGNv"
    "bm5lY3Rpb24gaGFzIGFscmVhZHkgYmVlbiBnaXZlbiB1cCBvbiBmb3IgZXhjZWVkaW5nIHRoZSBj"
    "YXAuCiAgICAgICAgc2VsZi5fcUJ5dGVzID0gMAogICAgICAgIHNlbGYuX3FMb2NrID0gdGhyZWFk"
    "aW5nLkxvY2soKQogICAgICAgIHNlbGYuX292ZXJmbG93ZWQgPSBGYWxzZQogICAgICAgIHNlbGYu"
    "dXNlciA9IE5vbmUKICAgICAgICBzZWxmLmd1aWQgPSBOb25lCiAgICAgICAgc2VsZi5kYXRhID0g"
    "YicnCiAgICAgICAgc2VsZi5TSyA9IGJ5dGVhcnJheShzdHJ1Y3QucGFjaygnPElJJywgMHhBNkFF"
    "MUY5QiwgMHg0MzhERkY0MCkpCiAgICAgICAgI1NlcmlhbGlzZXMgdGhlIHJhdyBzb2NrZXQgd3Jp"
    "dGVzLiBUaHJlZSB0aHJlYWRzIGNhbiB3YW50IHRvIHdyaXRlIHRvCiAgICAgICAgI29uZSBjbGll"
    "bnQ6IHRoaXMgY29ubmVjdGlvbidzIG93biByZWFkIGxvb3AgKGR1cmluZyB0aGUgaGFuZHNoYWtl"
    "KSwKICAgICAgICAjaXRzIHdyaXRlciB0aHJlYWQsIGFuZCB0aGUgR1VJIHRocmVhZCB2aWEga2lj"
    "a1BsYXllcigpLiBXaXRob3V0IHRoZQogICAgICAgICNsb2NrIHR3byBzZW5kYWxsKCkgY2FsbHMg"
    "Y2FuIGludGVybGVhdmUgYW5kIHNwbGl0IGEgcGFja2V0IGRvd24gdGhlCiAgICAgICAgI21pZGRs"
    "ZSwgd2hpY2ggdGhlIGNsaWVudCBzZWVzIGFzIHByb3RvY29sIGdhcmJhZ2UuCiAgICAgICAgc2Vs"
    "Zi5fc2VuZExvY2sgPSB0aHJlYWRpbmcuTG9jaygpCiAgICAgICAgc2VsZi5fd3JpdGVyID0gTm9u"
    "ZQogICAgICAgIHNlbGYuX3dyaXRlckRlYWQgPSB0aHJlYWRpbmcuRXZlbnQoKQogICAgICAgICNT"
    "ZXQgd2hlbiB0aGlzIGNvbm5lY3Rpb24gaGFzIGJlZW4gZ2l2ZW4gdXAgb24gZnJvbSAqb3V0c2lk"
    "ZSogaXRzIG93bgogICAgICAgICNoYW5kbGVyIHRocmVhZCAtIGFuIGFkbWluIGtpY2ssIG9yIHRo"
    "ZSBzZW5kLWJhY2tsb2cgY2FwLiBTaHV0dGluZyB0aGUKICAgICAgICAjc29ja2V0IGRvd24gaXMg"
    "c3VwcG9zZWQgdG8gd2FrZSB0aGF0IHRocmVhZCBvbiBpdHMgb3duLCBhbmQgbm9ybWFsbHkKICAg"
    "ICAgICAjZG9lczsgdGhpcyBtYWtlcyBpdCBjZXJ0YWluIHJhdGhlciB0aGFuIGRlcGVuZGVudCBv"
    "biB0aGUgc29ja2V0CiAgICAgICAgI3JlcG9ydGluZyB0aGUgc2h1dGRvd24gcHJvbXB0bHkuIEEg"
    "a2ljayB0aGF0IGlzIG5vdCBub3RpY2VkIGxlYXZlcyB0aGUKICAgICAgICAjYWNjb3VudCBjbGFp"
    "bWVkLCBhbmQgdGhlIHBsYXllciBjYW5ub3QgZ2V0IGJhY2sgaW4gdW50aWwgdGhlIGlkbGUKICAg"
    "ICAgICAjdGltZW91dCBleHBpcmVzIC0gdGhlIGV4YWN0IGZhaWx1cmUgYSBraWNrIGlzIG1lYW50"
    "IHRvIHJlc29sdmUuCiAgICAgICAgc2VsZi5fZHJvcHBlZCA9IHRocmVhZGluZy5FdmVudCgpCiAg"
    "ICAgICAgc2VsZi5fbGFzdFJlY3YgPSB0aW1lLm1vbm90b25pYygpCiAgICAgICAgc2VsZi5zZXJ2"
    "ZXIucmVnaXN0ZXJDb25uZWN0aW9uKHNlbGYpCiAgICAgICAgdHJ5OgogICAgICAgICAgICAjTmFn"
    "bGUgYmF0Y2hlcyBzbWFsbCB3cml0ZXMgYnkgaG9sZGluZyB0aGVtIGZvciB1cCB0byB+NDBtcyB3"
    "YWl0aW5nCiAgICAgICAgICAgICNmb3IgbW9yZSBkYXRhLiBFdmVyeSBtZXNzYWdlIHRoaXMgc2Vy"
    "dmVyIHNlbmRzIGlzIHNtYWxsIGFuZAogICAgICAgICAgICAjbGF0ZW5jeS1zZW5zaXRpdmUgLSBj"
    "aGF0LCBwb3NpdGlvbiB1cGRhdGVzIGFuZCBhYm92ZSBhbGwgdGhlCiAgICAgICAgICAgICMvZ2Ft"
    "ZWNvbW1hbmR0b3VzZXIgcmVsYXkgdGhhdCBjYXJyaWVzIHRoZSBhY3R1YWwgaW4tZ2FtZSBjby1v"
    "cAogICAgICAgICAgICAjdHJhZmZpYyBiZXR3ZWVuIHR3byBwbGF5ZXJzIC0gc28gdGhlIGRlbGF5"
    "IGlzIHB1cmUgYWRkZWQgbGFnLgogICAgICAgICAgICBzZWxmLnJlcXVlc3Quc2V0c29ja29wdChz"
    "b2NrZXQuSVBQUk9UT19UQ1AsIHNvY2tldC5UQ1BfTk9ERUxBWSwgMSkKICAgICAgICBleGNlcHQg"
    "T1NFcnJvcjoKICAgICAgICAgICAgcGFzcyAjbm90IGZhdGFsLCBqdXN0IHNsb3dlcgogICAgICAg"
    "IHRyeToKICAgICAgICAgICAgI0FzayB0aGUgT1MgdG8gcHJvYmUgYW4gaWRsZSBjb25uZWN0aW9u"
    "LiBXaGVuIGEgcGxheWVyJ3MgZ2FtZQogICAgICAgICAgICAjY3Jhc2hlcyBvdXRyaWdodCB0aGUg"
    "c29ja2V0IGlzIHVzdWFsbHkgcmVzZXQgYW5kIHdlIGZpbmQgb3V0IGF0CiAgICAgICAgICAgICNv"
    "bmNlLCBidXQgYSBtYWNoaW5lIHRoYXQgZnJlZXplcywgc2xlZXBzIG9yIGxvc2VzIGl0cyBsaW5r"
    "IHNlbmRzCiAgICAgICAgICAgICNub3RoaW5nIGF0IGFsbDogd2l0aG91dCBwcm9iZXMgdGhhdCBj"
    "b25uZWN0aW9uIHNpdHMgdGhlcmUgaG9sZGluZwogICAgICAgICAgICAjdGhlIGFjY291bnQgKCJB"
    "Y2NvdW50IGFscmVhZHkgbG9nZ2VkIGluIikgYW5kIGl0cyByb29tIHVudGlsIHRoZQogICAgICAg"
    "ICAgICAjaWRsZSB0aW1lb3V0IGV4cGlyZXMgbWludXRlcyBsYXRlci4gUHJvYmUgYWZ0ZXIgMzBz"
    "IGlkbGUsIHRoZW4KICAgICAgICAgICAgI2V2ZXJ5IDVzLgogICAgICAgICAgICBzZWxmLnJlcXVl"
    "c3Quc2V0c29ja29wdChzb2NrZXQuU09MX1NPQ0tFVCwgc29ja2V0LlNPX0tFRVBBTElWRSwgMSkK"
    "ICAgICAgICAgICAgaWYgaGFzYXR0cihzZWxmLnJlcXVlc3QsICdpb2N0bCcpIGFuZCBoYXNhdHRy"
    "KHNvY2tldCwgJ1NJT19LRUVQQUxJVkVfVkFMUycpOgogICAgICAgICAgICAgICAgc2VsZi5yZXF1"
    "ZXN0LmlvY3RsKHNvY2tldC5TSU9fS0VFUEFMSVZFX1ZBTFMsICgxLCAzMDAwMCwgNTAwMCkpICNX"
    "aW5kb3dzCiAgICAgICAgICAgIGVsc2U6CiAgICAgICAgICAgICAgICBmb3IgKG9wdCwgdmFsKSBp"
    "biAoKCdUQ1BfS0VFUElETEUnLCAzMCksICgnVENQX0tFRVBJTlRWTCcsIDUpLAogICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICgnVENQX0tFRVBDTlQnLCA0KSk6CiAgICAgICAgICAg"
    "ICAgICAgICAgaWYgaGFzYXR0cihzb2NrZXQsIG9wdCk6CiAgICAgICAgICAgICAgICAgICAgICAg"
    "IHNlbGYucmVxdWVzdC5zZXRzb2Nrb3B0KHNvY2tldC5JUFBST1RPX1RDUCwgZ2V0YXR0cihzb2Nr"
    "ZXQsIG9wdCksIHZhbCkKICAgICAgICBleGNlcHQgT1NFcnJvcjoKICAgICAgICAgICAgcGFzcyAj"
    "a2VlcGFsaXZlIGlzIGFuIG9wdGltaXNhdGlvbiwgbm90IGEgcmVxdWlyZW1lbnQKICAgIGRlZiBz"
    "ZW5kUmF3KHNlbGYsIG1zZyk6CiAgICAgICAgI1RoZSBzaW5nbGUgZnVubmVsIGZvciBldmVyeSBi"
    "eXRlIGxlYXZpbmcgdGhlIHNlcnZlciBvbiB0aGlzIHNvY2tldC4KICAgICAgICB3aXRoIHNlbGYu"
    "X3NlbmRMb2NrOgogICAgICAgICAgICBzZWxmLnJlcXVlc3Quc2VuZGFsbChtc2cpCiAgICBkZWYg"
    "c2VuZChzZWxmLCBtc2cpOgogICAgICAgICNOb3JtYWwgcGF0aCBvbmNlIHRoZSBjb25uZWN0aW9u"
    "IGlzIGxpdmU6IGhhbmQgb2ZmIHRvIHRoZSB3cml0ZXIgdGhyZWFkCiAgICAgICAgI3NvIHRoZSBj"
    "YWxsZXIgKGEgY29tbWFuZCBoYW5kbGVyLCBvciB0aGUgZGlzdHJpYnV0b3IncyBmYW4tb3V0KSBu"
    "ZXZlcgogICAgICAgICNibG9ja3Mgb24gYSBzbG93IG9yIHN0YWxsZWQgY2xpZW50LgogICAgICAg"
    "IGlmIG5vdCBtc2c6CiAgICAgICAgICAgIHJldHVybgogICAgICAgIHdpdGggc2VsZi5fcUxvY2s6"
    "CiAgICAgICAgICAgIGlmIHNlbGYuX292ZXJmbG93ZWQ6CiAgICAgICAgICAgICAgICByZXR1cm4g"
    "I2FscmVhZHkgYmVpbmcgdG9ybiBkb3duLCBzdG9wIGFjY291bnRpbmcgZm9yIGl0CiAgICAgICAg"
    "ICAgIHNlbGYuX3FCeXRlcyArPSBsZW4obXNnKQogICAgICAgICAgICBvdmVyID0gc2VsZi5fcUJ5"
    "dGVzID4gX01BWF9TRU5EX0JBQ0tMT0cKICAgICAgICAgICAgc2VsZi5fb3ZlcmZsb3dlZCA9IG92"
    "ZXIKICAgICAgICBpZiBvdmVyOgogICAgICAgICAgICAjU2VlIF9NQVhfU0VORF9CQUNLTE9HLiBT"
    "aHV0dGluZyB0aGUgc29ja2V0IGRvd24gaXMgd2hhdCB0ZWxscyB0aGUKICAgICAgICAgICAgI3Jl"
    "YWQgbG9vcCB0byBydW4gdGhpcyBjb25uZWN0aW9uJ3Mgbm9ybWFsIGNsZWFudXAgcGF0aC4KICAg"
    "ICAgICAgICAgd2hvID0gc2VsZi51c2VyLm5hbWUgaWYgc2VsZi51c2VyIGVsc2Ugc2VsZi5jbGll"
    "bnRfYWRkcmVzc1swXQogICAgICAgICAgICBwcmludChmJ1tMb2JieV0ge3dob306IG92ZXIge19N"
    "QVhfU0VORF9CQUNLTE9HfSBieXRlcyBxdWV1ZWQgdW5yZWFkLCBkcm9wcGluZycpCiAgICAgICAg"
    "ICAgIHNlbGYuZHJvcCgpCiAgICAgICAgICAgIHJldHVybgogICAgICAgIHNlbGYuX3NRdWV1ZS5w"
    "dXQobXNnKQogICAgZGVmIGRyb3Aoc2VsZik6CiAgICAgICAgI0VuZCB0aGlzIGNvbm5lY3Rpb24g"
    "ZnJvbSBhbm90aGVyIHRocmVhZC4gRmxhZ2dpbmcgaXQgZmlyc3QgbWVhbnMgdGhlCiAgICAgICAg"
    "I3JlYWQgbG9vcCBiYWlscyBvdXQgYXQgaXRzIG5leHQgcGFzcyBubyBtYXR0ZXIgd2hhdCB0aGUg"
    "c29ja2V0IGRvZXM7CiAgICAgICAgI3RoZSBzaHV0ZG93biBpcyB3aGF0IHdha2VzIGl0IGZyb20g"
    "c2VsZWN0KCkgc3RyYWlnaHQgYXdheS4gSXRzIG93bgogICAgICAgICNoYW5kbGVyIHRocmVhZCBz"
    "dGlsbCBydW5zIHRoZSBub3JtYWwgZmluaXNoKCkvY2xlYW51cCBwYXRoLCBzbyB0aGUKICAgICAg"
    "ICAjYWNjb3VudCBpcyByZWxlYXNlZCBhbmQgdGhlIHRvd24gcm9zdGVyIHRpZGllZCBleGFjdGx5"
    "IGFzIG9uIGFueSBvdGhlcgogICAgICAgICNkaXNjb25uZWN0LiBOZXZlciBjbG9zZSgpIGhlcmUg"
    "LSBzZWUgY2xvc2VDb25uZWN0aW9ucygpLgogICAgICAgIHNlbGYuX2Ryb3BwZWQuc2V0KCkKICAg"
    "ICAgICB0cnk6CiAgICAgICAgICAgIHNlbGYucmVxdWVzdC5zaHV0ZG93bihzb2NrZXQuU0hVVF9S"
    "RFdSKQogICAgICAgIGV4Y2VwdCBPU0Vycm9yOgogICAgICAgICAgICBwYXNzICNhbHJlYWR5IGdv"
    "bmUsIG9yIG5ldmVyIGZ1bGx5IGNvbm5lY3RlZAogICAgZGVmIGZsdXNoUGVuZGluZyhzZWxmLCB0"
    "aW1lb3V0KToKICAgICAgICAjQmVzdC1lZmZvcnQsIHN0cmljdGx5IGJvdW5kZWQgd2FpdCBmb3Ig"
    "dGhlIG91dGJvdW5kIHF1ZXVlIHRvIGRyYWluLgogICAgICAgICNGb3IgY2FsbGVycyB0aGF0IHdh"
    "bnQgYSBsYXN0IG1lc3NhZ2UgdG8gaGF2ZSBsZWZ0IGJlZm9yZSB0aGUgc29ja2V0CiAgICAgICAg"
    "I2dvZXMgZG93biAodGhlIGFkbWluIGtpY2spIHdpdGhvdXQgaW5oZXJpdGluZyBhIHN0YWxsZWQg"
    "cGVlcidzIHN0YWxsLgogICAgICAgIGRlYWRsaW5lID0gdGltZS5tb25vdG9uaWMoKSArIHRpbWVv"
    "dXQKICAgICAgICB3aGlsZSBub3Qgc2VsZi5fc1F1ZXVlLmVtcHR5KCkgYW5kIHRpbWUubW9ub3Rv"
    "bmljKCkgPCBkZWFkbGluZToKICAgICAgICAgICAgdGltZS5zbGVlcCgwLjAyKQogICAgZGVmIF93"
    "cml0ZXJMb29wKHNlbGYpOgogICAgICAgICNCbG9ja3Mgb24gdGhlIHF1ZXVlIGluc3RlYWQgb2Yg"
    "YmVpbmcgcG9sbGVkLiBQcmV2aW91c2x5IHRoZSByZWFkIGxvb3AKICAgICAgICAjZHJhaW5lZCB0"
    "aGlzIHF1ZXVlIGl0c2VsZiBiZXR3ZWVuIHJlY3YoKSB0aW1lb3V0cywgc28gYW55dGhpbmcgcXVl"
    "dWVkCiAgICAgICAgI2p1c3QgYWZ0ZXIgdGhlIHRocmVhZCB3ZW50IGJhY2sgaW50byByZWN2KCkg"
    "d2FpdGVkIG91dCB0aGUgZnVsbAogICAgICAgICN0aW1lb3V0IC0gdXAgdG8gMTAwbXMgb2YgbGF0"
    "ZW5jeSBhZGRlZCB0byBldmVyeSByZWxheWVkIGdhbWUgY29tbWFuZCwKICAgICAgICAjb24gdG9w"
    "IG9mIGV2ZXJ5IGlkbGUgY29ubmVjdGlvbiB3YWtpbmcgMTAgdGltZXMgYSBzZWNvbmQgdG8gY2hl"
    "Y2suCiAgICAgICAgdHJ5OgogICAgICAgICAgICB3aGlsZSBUcnVlOgogICAgICAgICAgICAgICAg"
    "bXNnID0gc2VsZi5fc1F1ZXVlLmdldCgpCiAgICAgICAgICAgICAgICBpZiBtc2cgaXMgc2VsZi5f"
    "U1RPUFdSSVRFUjoKICAgICAgICAgICAgICAgICAgICBicmVhawogICAgICAgICAgICAgICAgI0Nv"
    "YWxlc2NlIHdoYXRldmVyIGVsc2UgcGlsZWQgdXAgYmVoaW5kIGl0IGludG8gYSBzaW5nbGUgd3Jp"
    "dGUuCiAgICAgICAgICAgICAgICAjUG9zaXRpb24gYnJvYWRjYXN0cyBhbmQgZ2FtZSBjb21tYW5k"
    "cyBvZnRlbiBhcnJpdmUgaW4gYnVyc3RzLgogICAgICAgICAgICAgICAgY2h1bmtzID0gW21zZ10K"
    "ICAgICAgICAgICAgICAgIHN0b3BwaW5nID0gRmFsc2UKICAgICAgICAgICAgICAgIHdoaWxlIFRy"
    "dWU6CiAgICAgICAgICAgICAgICAgICAgdHJ5OgogICAgICAgICAgICAgICAgICAgICAgICBueHQg"
    "PSBzZWxmLl9zUXVldWUuZ2V0X25vd2FpdCgpCiAgICAgICAgICAgICAgICAgICAgZXhjZXB0IEV4"
    "Y2VwdGlvbjoKICAgICAgICAgICAgICAgICAgICAgICAgYnJlYWsKICAgICAgICAgICAgICAgICAg"
    "ICBpZiBueHQgaXMgc2VsZi5fU1RPUFdSSVRFUjoKICAgICAgICAgICAgICAgICAgICAgICAgc3Rv"
    "cHBpbmcgPSBUcnVlCiAgICAgICAgICAgICAgICAgICAgICAgIGJyZWFrCiAgICAgICAgICAgICAg"
    "ICAgICAgY2h1bmtzLmFwcGVuZChueHQpCiAgICAgICAgICAgICAgICBwYXlsb2FkID0gYicnLmpv"
    "aW4oY2h1bmtzKQogICAgICAgICAgICAgICAgI1JlbGVhc2VkIGJlZm9yZSB0aGUgd3JpdGUsIG5v"
    "dCBhZnRlcjogdGhlIGJhY2tsb2cgZXhpc3RzIHRvCiAgICAgICAgICAgICAgICAjZGVzY3JpYmUg"
    "d2hhdCBpcyBzdGlsbCB3YWl0aW5nIGZvciB0aGUgc29ja2V0LCBhbmQgdGhlc2UgYnl0ZXMKICAg"
    "ICAgICAgICAgICAgICNhcmUgb24gdGhlaXIgd2F5IG91dC4gQ291bnRpbmcgdGhlbSBhcyBwZW5k"
    "aW5nIGZvciB0aGUgd2hvbGUKICAgICAgICAgICAgICAgICNkdXJhdGlvbiBvZiBhIHNsb3cgc2Vu"
    "ZGFsbCgpIHdvdWxkIG1ha2UgYSBtZXJlbHkgc2xvdyBsaW5rIGxvb2sKICAgICAgICAgICAgICAg"
    "ICNsaWtlIHRoZSB3ZWRnZWQgY2xpZW50IHRoZSBjYXAgaXMgdGhlcmUgdG8gY2F0Y2guCiAgICAg"
    "ICAgICAgICAgICB3aXRoIHNlbGYuX3FMb2NrOgogICAgICAgICAgICAgICAgICAgIHNlbGYuX3FC"
    "eXRlcyAtPSBsZW4ocGF5bG9hZCkKICAgICAgICAgICAgICAgIHNlbGYuc2VuZFJhdyhwYXlsb2Fk"
    "KQogICAgICAgICAgICAgICAgaWYgc3RvcHBpbmc6CiAgICAgICAgICAgICAgICAgICAgcmV0dXJu"
    "CiAgICAgICAgZXhjZXB0IChDb25uZWN0aW9uUmVzZXRFcnJvciwgQ29ubmVjdGlvbkFib3J0ZWRF"
    "cnJvciwgQnJva2VuUGlwZUVycm9yLCBPU0Vycm9yKToKICAgICAgICAgICAgcGFzcyAjcGVlciBp"
    "cyBnb25lOyB0aGUgcmVhZCBsb29wIG5vdGljZXMgYW5kIHJ1bnMgdGhlIGNsZWFudXAKICAgICAg"
    "ICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICBwcmludCgnW0xvYmJ5XSBXcml0ZXIgZXJy"
    "b3I6XG4nICsgdHJhY2ViYWNrLmZvcm1hdF9leGMoKSkKICAgICAgICBmaW5hbGx5OgogICAgICAg"
    "ICAgICBzZWxmLl93cml0ZXJEZWFkLnNldCgpCiAgICBkZWYgX3N0YXJ0V3JpdGVyKHNlbGYpOgog"
    "ICAgICAgIHNlbGYuX3dyaXRlciA9IHRocmVhZGluZy5UaHJlYWQodGFyZ2V0PXNlbGYuX3dyaXRl"
    "ckxvb3AsIGRhZW1vbj1UcnVlKQogICAgICAgIHNlbGYuX3dyaXRlci5zdGFydCgpCiAgICBkZWYg"
    "X3N0b3BXcml0ZXIoc2VsZik6CiAgICAgICAgaWYgc2VsZi5fd3JpdGVyIGlzIE5vbmU6CiAgICAg"
    "ICAgICAgIHJldHVybgogICAgICAgIHNlbGYuX3NRdWV1ZS5wdXQoc2VsZi5fU1RPUFdSSVRFUikK"
    "ICAgICAgICBzZWxmLl93cml0ZXIuam9pbih0aW1lb3V0PTIuMCkKICAgICAgICBzZWxmLl93cml0"
    "ZXIgPSBOb25lCiAgICBkZWYgX2NsYWltU2Vzc2lvbihzZWxmKToKICAgICAgICAjVGFrZSBvd25l"
    "cnNoaXAgb2YgdGhlIHVzZXJuYW1lIHNsb3QgYmVmb3JlIHRlbGxpbmcgdGhlIGNsaWVudCBpdCBp"
    "cwogICAgICAgICNsb2dnZWQgaW4uIFJldHVybnMgRmFsc2UgaWYgYW5vdGhlciBjb25uZWN0aW9u"
    "IGdvdCB0aGVyZSBmaXJzdC4KICAgICAgICBpZiBzZWxmLnNlcnZlci5zdGF0ZS5jbGFpbVVzZXIo"
    "c2VsZi51c2VyLm5hbWUsIHNlbGYpOgogICAgICAgICAgICByZXR1cm4gVHJ1ZQogICAgICAgIHNl"
    "bGYudXNlci5kaXNjb25uZWN0KHNlbGYuc2VydmVyKSAjcmVsZWFzZXMgdGhlIGlkbnVtIHdlIGp1"
    "c3QgYWxsb2NhdGVkCiAgICAgICAgc2VsZi51c2VyID0gTm9uZQogICAgICAgIHJldHVybiBGYWxz"
    "ZQogICAgZGVmIGF0dGVtcHRMb2dpbihzZWxmLCB1c2VybmFtZSwgcGFzc3dvcmQpOgogICAgICAg"
    "IGlmIGxlbih1c2VybmFtZSk8MToKICAgICAgICAgICAgcmV0dXJuIDQgI05vIFVzZXJuYW1lLCBs"
    "aWtlbHkgZnJlc2ggbG9naW4KICAgICAgICAgICAgI1RPRE8gY2hlY2sgaWYgc2VyaWFsIGV4aXN0"
    "cyBhbmQgcmV0dXJuIHVzZXJuYW1lIHByb3Blcmx5CiAgICAgICAgaWYgbGVuKHBhc3N3b3JkKTwx"
    "OgogICAgICAgICAgICByZXR1cm4gMyAjUGFzc3dvcmQgdG9vIHNob3J0CiAgICAgICAgI1Rlc3Qg"
    "aWYgcGxheWVyIGFscmVhZHkgbG9nZ2VkIGluIChmYXN0IHBhdGg7IHRoZSBhdXRob3JpdGF0aXZl"
    "LAogICAgICAgICNyYWNlLWZyZWUgY2hlY2sgaXMgdGhlIGNsYWltVXNlcigpIGJlbG93KQogICAg"
    "ICAgIGlmIHNlbGYuc2VydmVyLmdldFBsYXllcih1c2VybmFtZSk6CiAgICAgICAgICAgIHJldHVy"
    "biAyICNUT0RPIFBMQVlFUiBMT0dHRUQgSU4gRVJST1IKICAgICAgICAjcGxheWVyIG5vdCBjdXJy"
    "ZW50bHkgbG9nZ2VkIGluLCBhdHRlbXB0IHRvIGxvZ2luIHZpYSBkYXRhIGhhbmRsZXIKICAgICAg"
    "ICBzZWxmLnVzZXIgPSBHREgubG9naW5QbGF5ZXIodXNlcm5hbWUsIHNlbGYsIHBhc3N3b3JkKQog"
    "ICAgICAgIGlmIHNlbGYudXNlcjoKICAgICAgICAgICAgcmV0dXJuIDAgaWYgc2VsZi5fY2xhaW1T"
    "ZXNzaW9uKCkgZWxzZSAyCiAgICAgICAgcmV0dXJuIDEgI1RPRE8gR2V0IGZyb20gR0RILmxvZ2lu"
    "UGxheWVyLCBwYXNzIHVzZXIgb2JqZWN0IGFsb25nPwogICAgZGVmIGF0dGVtcHRSZWdpc3Rlcihz"
    "ZWxmLCB1c2VybmFtZSwgcGFzc3dvcmQsIGVtYWlsLCBsb2NhdGlvbiwgYWdlLCBnZW5kZXIsIGRl"
    "c2NyaXB0aW9uKToKICAgICAgICAjVGVzdCBpZiBwbGF5ZXIgYWxyZWFkeSBsb2dnZWQgaW4KICAg"
    "ICAgICBpZiBzZWxmLnNlcnZlci5nZXRQbGF5ZXIodXNlcm5hbWUpOgogICAgICAgICAgICByZXR1"
    "cm4gMSAjVE9ETyBQTEFZRVIgTE9HR0VEIElOIEVSUk9SCiAgICAgICAgc2VsZi51c2VyID0gR0RI"
    "LnJlZ2lzdGVyUGxheWVyKHVzZXJuYW1lLCBzZWxmLCBwYXNzd29yZCwgZW1haWwsIGxvY2F0aW9u"
    "LCBhZ2UsIGdlbmRlciwgZGVzY3JpcHRpb24pCiAgICAgICAgaWYgc2VsZi51c2VyOgogICAgICAg"
    "ICAgICByZXR1cm4gMCBpZiBzZWxmLl9jbGFpbVNlc3Npb24oKSBlbHNlIDEKICAgICAgICByZXR1"
    "cm4gMiAjVE9ETyBnZXQgZXJyb3IgZnJvbSBHREgKICAgIGRlZiBoYW5kbGUoc2VsZik6CiAgICAg"
    "ICAgdHJ5OiAjSW50ZXJjZXB0IGFuZCBwcmludCBlcnJvcnMgZm9yIGRlYnVnZ2luZwogICAgICAg"
    "ICAgICBzZWxmLl9oYW5kbGUoKQogICAgICAgICAgICAjVE9ETyBsb29wIGxvYmJ5IGhhbmRsZSBi"
    "ZXR0ZXIgdG8gaGFuZGxlIGV4Y2VwdGlvbnMgZ3JhY2VmdWxseQogICAgICAgICAgICBzZWxmLl9s"
    "b2JieUhhbmRsZSgpCiAgICAgICAgZXhjZXB0IFByb3RvY29sRXJyb3IgYXMgZToKICAgICAgICAg"
    "ICAgI21hbGZvcm1lZC9vdmVyc2l6ZWQgaW5wdXQgLSB0aGUgY2xpZW50J3MgZmF1bHQsIG5vdCBv"
    "dXJzLiBEcm9wIHRoZQogICAgICAgICAgICAjY29ubmVjdGlvbiB3aXRoIG9uZSBsaW5lIGluc3Rl"
    "YWQgb2YgYSB0cmFjZWJhY2suCiAgICAgICAgICAgIHdobyA9IHNlbGYudXNlci5uYW1lIGlmIHNl"
    "bGYudXNlciBlbHNlIHNlbGYuY2xpZW50X2FkZHJlc3NbMF0KICAgICAgICAgICAgcHJpbnQoZidb"
    "TG9iYnldIFByb3RvY29sIGVycm9yIGZyb20ge3dob306IHtlfScpCiAgICAgICAgZXhjZXB0ICh6"
    "bGliLmVycm9yLCBzdHJ1Y3QuZXJyb3IsIFVuaWNvZGVEZWNvZGVFcnJvcikgYXMgZToKICAgICAg"
    "ICAgICAgI3RydW5jYXRlZC9nYXJiYWdlIHBhY2tldDogcGFyc2VEc3RyIGFuZCBzdHJ1Y3QudW5w"
    "YWNrIGJvdGggcmFpc2Ugb24KICAgICAgICAgICAgI3Nob3J0IHJlYWRzLCBhbmQgLmRlY29kZSgp"
    "IG9uIG5vbi1hc2NpaSBqdW5rLiBTYW1lIGNhdGVnb3J5LgogICAgICAgICAgICBwcmludChmJ1tM"
    "b2JieV0gTWFsZm9ybWVkIHBhY2tldCBmcm9tIHtzZWxmLmNsaWVudF9hZGRyZXNzWzBdfTogJwog"
    "ICAgICAgICAgICAgICAgICBmJ3t0eXBlKGUpLl9fbmFtZV9ffToge2V9JykKICAgICAgICBleGNl"
    "cHQgKENvbm5lY3Rpb25SZXNldEVycm9yLCBDb25uZWN0aW9uQWJvcnRlZEVycm9yLCBPU0Vycm9y"
    "KSBhcyBlOgogICAgICAgICAgICAjIGV4cGVjdGVkIGZvcm0gb2YgZGlzY29ubmVjdGlvbiAoaW5j"
    "bHVkaW5nIGEgZm9yY2VkIGFkbWluIGtpY2spLAogICAgICAgICAgICAjIGJ1dCBsZWF2ZSBhIG9u"
    "ZS1saW5lIGJyZWFkY3J1bWIgcmF0aGVyIHRoYW4gc3RheWluZyBmdWxseSBzaWxlbnQKICAgICAg"
    "ICAgICAgaWYgc2VsZi51c2VyOgogICAgICAgICAgICAgICAgcHJpbnQoZidbTG9iYnldIENvbm5l"
    "Y3Rpb24gY2xvc2VkIGZvciB7c2VsZi51c2VyLm5hbWV9OiB7ZX0nKQogICAgICAgIGV4Y2VwdCBF"
    "eGNlcHRpb246IyBhcyBlOgogICAgICAgICAgICBwcmludCh0cmFjZWJhY2suZm9ybWF0X2V4Yygp"
    "KQogICAgICAgICAgICBpZiBzZWxmLnVzZXI6CiAgICAgICAgICAgICAgICBwcmludChmJ1VzZXI6"
    "IHtzZWxmLnVzZXIubmFtZX0nKQogICAgICAgICAgICAjcmFpc2UgZQogICAgZGVmIF9sb2JieUhh"
    "bmRsZShzZWxmKToKICAgICAgICAjYWN0aXZlVXNlcnNbLi4uXSA9IHNlbGYgdXNlZCB0byBoYXBw"
    "ZW4gaGVyZTsgaXQgbm93IGhhcHBlbnMgdW5kZXIgYQogICAgICAgICNsb2NrIGluc2lkZSBhdHRl"
    "bXB0TG9naW4vYXR0ZW1wdFJlZ2lzdGVyLCBiZWZvcmUgdGhlIHdlbGNvbWUgcGFja2V0CiAgICAg"
    "ICAgI2dvZXMgb3V0LCBzbyB0d28gbG9naW5zIGZvciBvbmUgYWNjb3VudCBjYW4ndCBib3RoIHN1"
    "Y2NlZWQuCiAgICAgICAgcHJpbnQoZidVc2VyOiB7c2VsZi51c2VyLm5hbWV9IENvbm5lY3RlZCcp"
    "CiAgICAgICAgI0Zyb20gaGVyZSBvbiBub3RoaW5nIHdyaXRlcyB0byB0aGUgc29ja2V0IGlubGlu"
    "ZTogdGhlIHdyaXRlciB0aHJlYWQKICAgICAgICAjb3ducyB0aGUgb3V0Ym91bmQgZGlyZWN0aW9u"
    "IGFuZCB0aGlzIGxvb3Agb25seSByZWFkcy4KICAgICAgICBzZWxmLl9zdGFydFdyaXRlcigpCiAg"
    "ICAgICAgc2VsZi5fbGFzdFJlY3YgPSB0aW1lLm1vbm90b25pYygpCiAgICAgICAgI1RoZSBzb2Nr"
    "ZXQgc3RheXMgaW4gYmxvY2tpbmcgbW9kZSBmb3IgaXRzIHdob2xlIGxpZmUgZnJvbSBoZXJlIG9u"
    "LCBhbmQKICAgICAgICAjcmVhZGluZXNzIGlzIHdhaXRlZCBmb3Igd2l0aCBzZWxlY3QoKSBpbnN0"
    "ZWFkIG9mIGEgc29ja2V0IHRpbWVvdXQuCiAgICAgICAgI1RoaXMgaXMgbm90IGEgc3R5bGUgcHJl"
    "ZmVyZW5jZSAtIGEgc29ja2V0IHRpbWVvdXQgaXMgYSBwcm9wZXJ0eSBvZiB0aGUKICAgICAgICAj"
    "KnNvY2tldCosIG5vdCBvZiB0aGUgY2FsbCwgc28gdGhlIHNldHRpbWVvdXQoX1JFQURfVElNRU9V"
    "VCkgdGhpcyBsb29wCiAgICAgICAgI3VzZWQgdG8gZG8gb24gZXZlcnkgcGFzcyBhbHNvIGFybWVk"
    "IGEgMXMgdGltZW91dCBvbiB0aGUgd3JpdGVyCiAgICAgICAgI3RocmVhZCdzIGNvbmN1cnJlbnQg"
    "c2VuZGFsbCgpLiBBIGNsaWVudCB3aG9zZSByZWNlaXZlIHdpbmRvdyB3YXMgZnVsbAogICAgICAg"
    "ICNmb3IgYSBzZWNvbmQgKGV4YWN0bHkgdGhlIGNhc2UgZHVyaW5nIGEgYnVzeSBjby1vcCBzZXNz"
    "aW9uKSBtYWRlIHRoYXQKICAgICAgICAjc2VuZGFsbCgpIHJhaXNlIFRpbWVvdXRFcnJvciAqYWZ0"
    "ZXIgaGF2aW5nIGFscmVhZHkgd3JpdHRlbiBwYXJ0IG9mIHRoZQogICAgICAgICNwYWNrZXQqOiB0"
    "aGUgd3JpdGVyIHRocmVhZCBkaWVkLCBhbmQgd2hhdGV2ZXIgdGhlIGNsaWVudCBoYWQgcmVjZWl2"
    "ZWQKICAgICAgICAjd2FzIGhhbGYgYSBtZXNzYWdlLCBzbyBpdHMgY29tbWFuZCBzdHJlYW0gd2Fz"
    "IGRlc3luY2hyb25pc2VkIGZyb20KICAgICAgICAjdGhhdCBwb2ludCBvbi4gc2VsZWN0KCkgbGVh"
    "dmVzIHRoZSBzb2NrZXQgYmxvY2tpbmcsIHNvIHdyaXRlcyBhcmUKICAgICAgICAjbmV2ZXIgaW50"
    "ZXJydXB0ZWQsIHdoaWxlIHJlYWRzIHN0aWxsIHdha2UgdXAgcmVndWxhcmx5IGVub3VnaCB0bwog"
    "ICAgICAgICNub3RpY2Ugc2h1dGRvd24gYW5kIHRoZSBpZGxlIGRlYWRsaW5lLgogICAgICAgIHNl"
    "bGYucmVxdWVzdC5zZXR0aW1lb3V0KE5vbmUpCiAgICAgICAgd2hpbGUgVHJ1ZToKICAgICAgICAg"
    "ICAgaWYgc2VsZi5fZHJvcHBlZC5pc19zZXQoKToKICAgICAgICAgICAgICAgIGJyZWFrICNraWNr"
    "ZWQsIG9yIGRyb3BwZWQgZm9yIGFuIHVucmVhZCBzZW5kIGJhY2tsb2cKICAgICAgICAgICAgaWYg"
    "c2VsZi5fd3JpdGVyRGVhZC5pc19zZXQoKToKICAgICAgICAgICAgICAgIGJyZWFrICNwZWVyIHdl"
    "bnQgYXdheSB3aGlsZSB3ZSB3ZXJlIHNlbmRpbmcKICAgICAgICAgICAgaWYgc2VsZi5zZXJ2ZXIu"
    "X2lzX2Nsb3Npbmc6CiAgICAgICAgICAgICAgICBicmVhayAjc2VydmVyIGlzIHN0b3BwaW5nIC0g"
    "Y2hlY2tlZCBoZXJlLCBub3Qgb25seSBvbiBhbiBpZGxlCiAgICAgICAgICAgICAgICAgICAgICAj"
    "dGltZW91dCwgc28gYSBjbGllbnQgdGhhdCBrZWVwcyB0YWxraW5nIGNhbm5vdCBrZWVwIGl0cwog"
    "ICAgICAgICAgICAgICAgICAgICAgI2hhbmRsZXIgdGhyZWFkIChhbmQgaXRzIGxvZyBzcGFtKSBh"
    "bGl2ZSBwYXN0IHNodXRkb3duCiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgIHJlYWR5"
    "LCBfLCBfID0gc2VsZWN0LnNlbGVjdChbc2VsZi5yZXF1ZXN0XSwgW10sIFtdLCBfUkVBRF9USU1F"
    "T1VUKQogICAgICAgICAgICBleGNlcHQgKE9TRXJyb3IsIFZhbHVlRXJyb3IpOgogICAgICAgICAg"
    "ICAgICAgYnJlYWsgI3NvY2tldCBjbG9zZWQgdW5kZXIgdXMgKGFkbWluIGtpY2sgLyBzaHV0ZG93"
    "bikKICAgICAgICAgICAgaWYgbm90IHJlYWR5OgogICAgICAgICAgICAgICAgaWYgc2VsZi5zZXJ2"
    "ZXIuX2lzX2Nsb3Npbmc6CiAgICAgICAgICAgICAgICAgICAgYnJlYWsgI1NlcnZlciBTaHV0dGlu"
    "ZyBkb3duCiAgICAgICAgICAgICAgICBpZiBfSURMRV9USU1FT1VUIGFuZCAodGltZS5tb25vdG9u"
    "aWMoKSAtIHNlbGYuX2xhc3RSZWN2KSA+IF9JRExFX1RJTUVPVVQ6CiAgICAgICAgICAgICAgICAg"
    "ICAgI0hhbGYtb3BlbiBjb25uZWN0aW9uOiB0aGUgcGVlciBpcyB1bnJlYWNoYWJsZSBidXQgbmV2"
    "ZXIKICAgICAgICAgICAgICAgICAgICAjc2VudCBhIEZJTi9SU1QsIHNvIHJlY3YoKSBibG9ja3Mg"
    "Zm9yZXZlciBhbmQgdGhlIGFjY291bnQKICAgICAgICAgICAgICAgICAgICAjc3RheXMgY2xhaW1l"
    "ZC4gUmVhcCBpdCBzbyB0aGUgcGxheWVyIGNhbiBsb2cgYmFjayBpbi4KICAgICAgICAgICAgICAg"
    "ICAgICBwcmludChmJ1tMb2JieV0ge3NlbGYudXNlci5uYW1lfSBpZGxlIGZvciB7X0lETEVfVElN"
    "RU9VVH1zLCBkcm9wcGluZycpCiAgICAgICAgICAgICAgICAgICAgYnJlYWsKICAgICAgICAgICAg"
    "ICAgIGNvbnRpbnVlCiAgICAgICAgICAgIHJtc2cgPSBzZWxmLnJlcXVlc3QucmVjdihSRUNWX0JV"
    "Rl9MRU4pICNUT0RPIGxvZyBuZXR3b3JrIGJ5dGVyYXRlCiAgICAgICAgICAgIGlmIG5vdCBybXNn"
    "OgogICAgICAgICAgICAgICAgYnJlYWsgI0Rpc2Nvbm5lY3RlZAogICAgICAgICAgICBzZWxmLmRh"
    "dGErPXJtc2cKICAgICAgICAgICAgc2VsZi5fbGFzdFJlY3YgPSB0aW1lLm1vbm90b25pYygpCiAg"
    "ICAgICAgICAgIHdoaWxlIHNlbGYuZGF0YToKICAgICAgICAgICAgICAgIHRyeToKICAgICAgICAg"
    "ICAgICAgICAgICBjbWRfbCA9IHNlbGYuZGF0YS5pbmRleCgwKQogICAgICAgICAgICAgICAgZXhj"
    "ZXB0IFZhbHVlRXJyb3I6CiAgICAgICAgICAgICAgICAgICAgI3ByaW50KCdjbWQgZGVjb2RlIGVy"
    "cm9yOlxuJywgdHJhY2ViYWNrLmZvcm1hdF9leGMoKSkKICAgICAgICAgICAgICAgICAgICBicmVh"
    "azsjTWF5IHJlcXVpcmUgbW9yZSBkYXRhCiAgICAgICAgICAgICAgICBjbWQgPSB3aXJlX2RlY29k"
    "ZShzZWxmLmRhdGFbMDpjbWRfbF0pCiAgICAgICAgICAgICAgICBzZWxmLmRhdGEgPSBzZWxmLmRh"
    "dGFbY21kX2wrMTpdCiAgICAgICAgICAgICAgICByZXNwb25zZSA9IHNlbGYuc2VydmVyLmNvbXBh"
    "cnMucGFyc2UoY21kLCBzZWxmKQogICAgICAgICAgICAgICAgaWYgcmVzcG9uc2U6CiAgICAgICAg"
    "ICAgICAgICAgICAgI1F1ZXVlZCByYXRoZXIgdGhhbiBzZW50IGlubGluZSwgc28gdGhpcyBjb25u"
    "ZWN0aW9uIGhhcyBhCiAgICAgICAgICAgICAgICAgICAgI3NpbmdsZSBvcmRlcmVkIG91dGJvdW5k"
    "IHN0cmVhbS4gU2VuZGluZyBoZXJlIGRpcmVjdGx5CiAgICAgICAgICAgICAgICAgICAgI3dvdWxk"
    "IHJhY2UgdGhlIHdyaXRlciB0aHJlYWQgYW5kIGNvdWxkIGxhbmQgaW4gdGhlIG1pZGRsZQogICAg"
    "ICAgICAgICAgICAgICAgICNvZiBhIGJyb2FkY2FzdCBpdCBpcyBhbHJlYWR5IHdyaXRpbmcuCiAg"
    "ICAgICAgICAgICAgICAgICAgc2VsZi5zZW5kKHJlc3BvbnNlKQogICAgICAgICAgICAgICAgI0xv"
    "b3NlIGJsb2JzIHNob3VsZCBub3QgaGFwcGVuIGFueW1vcmUgaG9wZWZ1bGx5CiAgICAgICAgICAg"
    "ICAgICAjVE9ETyBmaXggdW5jb21wcmVzc2VkIGRhdGEgYmxvYnM/CiAgICAgICAgICAgICAgICAj"
    "VE9ETyBza2lwIDEgYnl0ZSBvbmx5IHdoZW4gZGVjb2RlIGVycm9yPwogICAgICAgICAgICAgICAg"
    "aWYgKGxlbihzZWxmLmRhdGEpPjIgYW5kCiAgICAgICAgICAgICAgICAgICAgICAgIHNlbGYuZGF0"
    "YVswXT09MHg3OCBhbmQKICAgICAgICAgICAgICAgICAgICAgICAgc2VsZi5kYXRhWzFdPT0weDlj"
    "KToKICAgICAgICAgICAgICAgICAgICAjTG9vc2UgdW5oYW5kbGVkIGJsb2IgYWZ0ZXIgY29tbWFu"
    "ZAogICAgICAgICAgICAgICAgICAgIGJsb2IsIHNlbGYuZGF0YSA9IHBfZ2V0QmxvYihzZWxmLmRh"
    "dGEsIHNlbGYucmVxdWVzdCkKICAgICAgICAgICAgICAgICAgICAjVGhlIG90aGVyIGJsaW5kIHNw"
    "b3Q6IGFueXRoaW5nIHRoZSBjbGllbnQgc2VuZHMgYXMgYQogICAgICAgICAgICAgICAgICAgICNj"
    "b21wcmVzc2VkIGJsb2IgcmF0aGVyIHRoYW4gYSB0ZXh0IGNvbW1hbmQgd2FzIHJlYWQgYW5kCiAg"
    "ICAgICAgICAgICAgICAgICAgI3Rocm93biBhd2F5IHdpdGhvdXQgYSB0cmFjZS4KICAgICAgICAg"
    "ICAgICAgICAgICBpZiBfREVCVUdfTE9HX0NPTU1BTkRTOgogICAgICAgICAgICAgICAgICAgICAg"
    "ICB3aG8gPSBzZWxmLnVzZXIubmFtZSBpZiBzZWxmLnVzZXIgZWxzZSAnPycKICAgICAgICAgICAg"
    "ICAgICAgICAgICAgcHJpbnQoZidbY21kXSB7d2hvfSAtPiAoVU5IQU5ETEVEIEJMT0IgYWZ0ZXIg"
    "e2NtZCFyfSkgJwogICAgICAgICAgICAgICAgICAgICAgICAgICAgICBmJ3tsZW4oYmxvYil9IGJ5"
    "dGVzJykKICAgIGRlZiBfcmVjdk1vcmUoc2VsZik6CiAgICAgICAgY2h1bmsgPSBzZWxmLnJlcXVl"
    "c3QucmVjdihSRUNWX0JVRl9MRU4pCiAgICAgICAgaWYgbm90IGNodW5rOgogICAgICAgICAgICAj"
    "cGVlciBkaXNjb25uZWN0ZWQgZHVyaW5nIGhhbmRzaGFrZS9sb2dpbiwgc3RvcCB0aGUgYnVzeS1s"
    "b29wCiAgICAgICAgICAgIHJhaXNlIENvbm5lY3Rpb25SZXNldEVycm9yKCdkaXNjb25uZWN0ZWQg"
    "ZHVyaW5nIGxvZ2luJykKICAgICAgICBzZWxmLmRhdGEgKz0gY2h1bmsKICAgIGRlZiBfaGFuZGxl"
    "KHNlbGYpOgogICAgICAgICNUT0RPIGxvZyBsb2dpbiBhdHRlbXB0cz8KICAgICAgICBwZWVyX2lw"
    "ID0gc2VsZi5jbGllbnRfYWRkcmVzc1swXQogICAgICAgIHByaW50KCdDb25uZWN0aW9uIGF0dGVt"
    "cHQgZnJvbTonLCBwZWVyX2lwKQogICAgICAgIExJUyA9IDIgI2xvZ2luIHN0YXRlICNUT0RPIGNv"
    "bnNpZGVyIGxvbmcgdGltZW91dHM/CiAgICAgICAgd2hpbGUgTElTOgogICAgICAgICAgICB3aGls"
    "ZSBsZW4oc2VsZi5kYXRhKTw0OgogICAgICAgICAgICAgICAgc2VsZi5fcmVjdk1vcmUoKQogICAg"
    "ICAgICAgICBwYWNrX2xlbiA9IHN0cnVjdC51bnBhY2soJzxJJyxzZWxmLmRhdGFbMDo0XSlbMF0K"
    "ICAgICAgICAgICAgaWYgcGFja19sZW4gPCA0IG9yIHBhY2tfbGVuID4gX01BWF9IQU5EU0hBS0U6"
    "CiAgICAgICAgICAgICAgICAjdW52YWxpZGF0ZWQsIHRoaXMgaXMgYSBwcmUtYXV0aGVudGljYXRp"
    "b24gbWVtb3J5IGJvbWI6IGFuCiAgICAgICAgICAgICAgICAjdW5hdXRoZW50aWNhdGVkIHBlZXIg"
    "YW5ub3VuY2VzIGEgNEdCIHBhY2tldCBhbmQgdGhlIGxvb3AgYmVsb3cKICAgICAgICAgICAgICAg"
    "ICNidWZmZXJzIHVudGlsIHRoZSBwcm9jZXNzIGRpZXMKICAgICAgICAgICAgICAgIHJhaXNlIFBy"
    "b3RvY29sRXJyb3IoZidoYW5kc2hha2UgcGFja2V0IGxlbmd0aCB7cGFja19sZW59IG91dCBvZiBy"
    "YW5nZScpCiAgICAgICAgICAgIHdoaWxlKGxlbihzZWxmLmRhdGEpPHBhY2tfbGVuKToKICAgICAg"
    "ICAgICAgICAgIHNlbGYuX3JlY3ZNb3JlKCkKICAgICAgICAgICAgI3NsaWNlIHRvIHBhY2tfbGVu"
    "IChub3QgdG8gdGhlIGVuZCBvZiB0aGUgYnVmZmVyKTogYW55dGhpbmcgcGFzdAogICAgICAgICAg"
    "ICAjdGhpcyBwYWNrZXQgYmVsb25ncyB0byB0aGUgbmV4dCBvbmUuIEJvdW5kZWQgZGVjb21wcmVz"
    "cywgYmVjYXVzZSBhCiAgICAgICAgICAgICM2NGsgaGFuZHNoYWtlIG9mIGNvbXByZXNzZWQgemVy"
    "b2VzIGV4cGFuZHMgdG8gaHVuZHJlZHMgb2YgTUIuCiAgICAgICAgICAgIHJlcyA9IF9kZWNvbXBy"
    "ZXNzX2JvdW5kZWQoc2VsZi5kYXRhWzQ6cGFja19sZW5dLCBfTUFYX0hBTkRTSEFLRV9JTkZMQVRF"
    "RCkKICAgICAgICAgICAgc2VsZi5kYXRhID0gc2VsZi5kYXRhW3BhY2tfbGVuOl0KICAgICAgICAg"
    "ICAgaWYgTElTID09IDI6CiAgICAgICAgICAgICAgICBnYW1ldmVyc2lvbiA9IHJlc1swOjE2XSAj"
    "VE9ETyBub3RlIGdhbWUgdmVyc2lvbiAodW52ZXJpZmllZCkgcGVyIHVzZXIKICAgICAgICAgICAg"
    "ICAgIGxhbmduYW1lLCBvZmYgPSBwYXJzZURzdHIocmVzLCAxNikKICAgICAgICAgICAgICAgICNU"
    "T0RPIGNvbnNpZGVyIFRXU0UgaW5kaWNhdG9yIHRvIGNyZWF0ZSBzZWN1cmUgY29ubmVjdGlvbj8K"
    "ICAgICAgICAgICAgICAgICNUT0RPIGNoZWNrIGlmIHZhbmlsbGEgc2VydmVyIGlnbm9yZXMgZXh0"
    "cmEgZGF0YSBpbiBoYW5kc2hha2UgcHJvY2VzcwogICAgICAgICAgICAgICAgUksgPSByZXNbb2Zm"
    "Kzg6b2ZmKzE2XQogICAgICAgICAgICAgICAgZm9yIGkgaW4gcmFuZ2UobGVuKFJLKSk6CiAgICAg"
    "ICAgICAgICAgICAgICAgc2VsZi5TS1tpXV49UktbaV0KICAgICAgICAgICAgICAgICN3YXMgaGFy"
    "ZGNvZGVkICdUVzFDUycgd2l0aCBhICJTRVJWRVIgTkFNRSBjZmdUT0RPIiBub3RlOiB0aGUKICAg"
    "ICAgICAgICAgICAgICNuYW1lIGNvbmZpZ3VyZWQgaW4gQ29uZmlnLmluaS90aGUgR1VJIHJlYWNo"
    "ZWQgdGhlIHdlbGNvbWUKICAgICAgICAgICAgICAgICNwYWNrZXQgYnV0IG5ldmVyIHRoaXMgb25l"
    "LCBzbyB0aGUgcHJlLWxvZ2luIGhhbmRzaGFrZSBhbHdheXMKICAgICAgICAgICAgICAgICNhbm5v"
    "dW5jZWQgdGhlIHBsYWNlaG9sZGVyLgogICAgICAgICAgICAgICAgc2VsZi5zZW5kUmF3KF9zZXJ2"
    "ZXJfaW5mb19wYWNrZXQoc2FuaXRpemVUZXh0KERFRkFVTFRfVElUTEUpKSkKICAgICAgICAgICAg"
    "ICAgICNUT0RPIFRXMUNTIGluZGljYXRvciBmb3IgVFdTRSBjbGllbnQgdG8gY3JlYXRlIHNlY3Vy"
    "ZSBjb25uZWN0aW9uIG9yIHByZS1oYXNoIHBhc3N3b3JkPwogICAgICAgICAgICAgICAgTElTID0g"
    "MSAKICAgICAgICAgICAgICAgIHNlbGYuU0sgPSBieXRlcyhzZWxmLlNLKQogICAgICAgICAgICBl"
    "bGlmIExJUyA9PSAxOgogICAgICAgICAgICAgICAgbG9naW5FcnJvciA9IC0xCiAgICAgICAgICAg"
    "ICAgICAjU3RhbGwgcmVwZWF0IG9mZmVuZGVycyBiZWZvcmUgZG9pbmcgYW55IFBCS0RGMiB3b3Jr"
    "IGZvciB0aGVtLgogICAgICAgICAgICAgICAgI1NsZWVwaW5nIGluIHRoaXMgaGFuZGxlciB0aHJl"
    "YWQgaXMgdGhlIHBvaW50OiBpdCBjb3N0cyB1cwogICAgICAgICAgICAgICAgI25vdGhpbmcgYW5k"
    "IHJhdGUtbGltaXRzIHRoYXQgY29ubmVjdGlvbi4KICAgICAgICAgICAgICAgIGRlbGF5ID0gTE9H"
    "SU5fVEhST1RUTEUuZGVsYXlGb3IocGVlcl9pcCkKICAgICAgICAgICAgICAgIGlmIGRlbGF5Ogog"
    "ICAgICAgICAgICAgICAgICAgIHRpbWUuc2xlZXAoZGVsYXkpCiAgICAgICAgICAgICAgICB1c2Vy"
    "bmFtZSwgb2ZmID0gcGFyc2VEc3RyKHJlcywgMCkKICAgICAgICAgICAgICAgIHBhc3N3b3JkLCBv"
    "ZmYgPSBwYXJzZURzdHIocmVzLCBvZmYpCiAgICAgICAgICAgICAgICAjVE9ETyBUV1NFIG1vZCBm"
    "b3IgaGlnaGVyIGxvZ2luIHNlY3VyaXR5CiAgICAgICAgICAgICAgICAjLWVuY3J5cHRlZCBjb25u"
    "ZWN0aW9uIHRvIHByZXZlbnQgcmVwbGF5IGF0dGFja3MKICAgICAgICAgICAgICAgICMtcHJlaGFz"
    "aCBwYXNzd29yZCB3aXRoIHNlcmlhbD8sIGNoZWNrIGlmIHJlY292ZXJ5IHBvc3NpYmxlLgogICAg"
    "ICAgICAgICAgICAgc2VsZi5ndWlkID0gYnl0ZXMocmVzW29mZjpvZmYrMTZdKQogICAgICAgICAg"
    "ICAgICAgI3ByaW50KCdndWlkIGJ5dGU6Jywgc2VsZi5ndWlkWzFdKQogICAgICAgICAgICAgICAg"
    "I3NlbGYuZ3VpZCA9IGJ5dGVhcnJheShyZXNbb2ZmOm9mZisxNl0pCiAgICAgICAgICAgICAgICAj"
    "c2VsZi5ndWlkWzFdXj0weDE2ICNETyBOT1QgcGVyZm9ybSBzZXJ2ZXJzaWRlCiAgICAgICAgICAg"
    "ICAgICAjc2VsZi5ndWlkID0gYnl0ZXMoc2VsZi5ndWlkKQogICAgICAgICAgICAgICAgb2ZmKz0x"
    "NgogICAgICAgICAgICAgICAgaXNyZWcgPSBzdHJ1Y3QudW5wYWNrKCc8SScscmVzW29mZjpvZmYr"
    "NF0pWzBdCiAgICAgICAgICAgICAgICBvZmYrPTQKICAgICAgICAgICAgICAgIHZpYVJlZ2lzdGVy"
    "ID0gYm9vbChpc3JlZykKICAgICAgICAgICAgICAgIGlmIGlzcmVnOgogICAgICAgICAgICAgICAg"
    "ICAgIGVtYWlsLCBvZmYgPSBwYXJzZURzdHIocmVzLCBvZmYpCiAgICAgICAgICAgICAgICAgICAg"
    "bG9jYXRpb24sIG9mZiA9IHBhcnNlRHN0cihyZXMsIG9mZikKICAgICAgICAgICAgICAgICAgICBh"
    "Z2UgPSByZXNbb2ZmXQogICAgICAgICAgICAgICAgICAgIGdlbmRlciA9IHJlc1tvZmYrMV0KICAg"
    "ICAgICAgICAgICAgICAgICBvZmYrPTIgI2FnZSwgZ2VuZGVyCiAgICAgICAgICAgICAgICAgICAg"
    "ZGVzY3JpcHRpb24sIG9mZiA9IHBhcnNlRHN0cihyZXMsIG9mZikKICAgICAgICAgICAgICAgICAg"
    "ICBsb2dpbkVycm9yID0gc2VsZi5hdHRlbXB0UmVnaXN0ZXIodXNlcm5hbWUsIHBhc3N3b3JkLCBl"
    "bWFpbCwgbG9jYXRpb24sIGFnZSwgZ2VuZGVyLCBkZXNjcmlwdGlvbikKICAgICAgICAgICAgICAg"
    "IGVsc2U6CiAgICAgICAgICAgICAgICAgICAgbG9naW5FcnJvciA9IHNlbGYuYXR0ZW1wdExvZ2lu"
    "KHVzZXJuYW1lLCBwYXNzd29yZCkKICAgICAgICAgICAgICAgICAgICBpZiBsb2dpbkVycm9yID09"
    "IDEgYW5kIF9BVVRPX1JFR0lTVEVSOgogICAgICAgICAgICAgICAgICAgICAgICB2aWFSZWdpc3Rl"
    "ciA9IFRydWUKICAgICAgICAgICAgICAgICAgICAgICAgbG9naW5FcnJvciA9IHNlbGYuYXR0ZW1w"
    "dFJlZ2lzdGVyKHVzZXJuYW1lLCBwYXNzd29yZCwgIiIsICIiLCAxLCAwLCAiIikKICAgICAgICAg"
    "ICAgICAgICAgICAgICAgaWYgbG9naW5FcnJvciBhbmQgR0RILm5hbWVUYWtlbih1c2VybmFtZSk6"
    "CiAgICAgICAgICAgICAgICAgICAgICAgICAgICAjVGhlIGFjY291bnQgZXhpc3RzLCBzbyB0aGlz"
    "IHdhcyBuZXZlciBhCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAjcmVnaXN0cmF0aW9uOiB0"
    "aGUgbG9naW4gYmVmb3JlIGl0IGZhaWxlZCBvbiB0aGUKICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICNwYXNzd29yZCBvciAtIGZhciBtb3JlIG9mdGVuIC0gb24gdGhlIHNlcmlhbCwKICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICNiZWNhdXNlIGFjY291bnRzIGFyZSBib3VuZCB0byB0aGUg"
    "a2V5IHRoZSBjbGllbnQKICAgICAgICAgICAgICAgICAgICAgICAgICAgICNoYW5kc2hha2VzIHdp"
    "dGggKHNlZSBsb2dpblBsYXllcidzIHN0cmljdCBsb29rdXApLgogICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgI0ZhbGxpbmcgdGhyb3VnaCB0byB0aGUgcmVnaXN0cmF0aW9uIHdvcmRpbmcgdG9s"
    "ZCBhCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAjcGxheWVyIHdobyBoYWQgcmVpbnN0YWxs"
    "ZWQgdGhlIGdhbWUgdGhhdCB0aGVpcgogICAgICAgICAgICAgICAgICAgICAgICAgICAgIyp1c2Vy"
    "bmFtZSogd2FzIGludmFsaWQsIHdoaWNoIHNlbnQgdGhlbSBvZmYKICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICNpbnZlbnRpbmcgbmV3IG5hbWVzIHRoYXQgY291bGQgbmV2ZXIgd29yay4KICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgIHZpYVJlZ2lzdGVyID0gRmFsc2UKICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgIGxvZ2luRXJyb3IgPSA1CiAgICAgICAgICAgICAgICBpZiBsb2dpbkVy"
    "cm9yID09IDA6CiAgICAgICAgICAgICAgICAgICAgTE9HSU5fVEhST1RUTEUucmVjb3JkU3VjY2Vz"
    "cyhwZWVyX2lwKQogICAgICAgICAgICAgICAgICAgICNUT0RPIGJldHRlciBoYW5kbGluZyBvZiBU"
    "SVRMRSBBTkQgTU9URAogICAgICAgICAgICAgICAgICAgIHNlbGYuc2VuZFJhdyhfc2VydmVyX3dl"
    "bGNvbWVfcGFja2V0KGJ5dGVzKHNlbGYuU0spLCBERUZBVUxUX1RJVExFLCBERUZBVUxUX01PVEQp"
    "KQogICAgICAgICAgICAgICAgICAgIExJUyA9IDAKICAgICAgICAgICAgICAgIGVsc2U6ICNlcnJv"
    "ciBiYXNlZCBvbiBsb2dpbkVycm9yIG51bWJlcgogICAgICAgICAgICAgICAgICAgIGNvdW50ID0g"
    "TE9HSU5fVEhST1RUTEUucmVjb3JkRmFpbHVyZShwZWVyX2lwKQogICAgICAgICAgICAgICAgICAg"
    "IGlmIGNvdW50ID09IF9MT0dJTl9GQUlMX0xJTUlUOgogICAgICAgICAgICAgICAgICAgICAgICBw"
    "cmludChmJ1tMb2JieV0gVGhyb3R0bGluZyBsb2dpbnMgZnJvbSB7cGVlcl9pcH0gJwogICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICBmJyh7Y291bnR9IGZhaWx1cmVzIGluIHtfTE9HSU5fRkFJ"
    "TF9XSU5ET1d9cyknKQogICAgICAgICAgICAgICAgICAgIGVycm1zZ3MgPSBfUkVHSVNURVJfRVJS"
    "T1JTIGlmIHZpYVJlZ2lzdGVyIGVsc2UgX0xPR0lOX0VSUk9SUwogICAgICAgICAgICAgICAgICAg"
    "IHNlbGYuc2VuZFJhdyhfaW5pdF9lcnJvcihlcnJtc2dzLmdldChsb2dpbkVycm9yLCAnTG9naW4g"
    "ZmFpbGVkJykpKQogICAgZGVmIGZpbmlzaChzZWxmKToKICAgICAgICBzZWxmLnNlcnZlci51bnJl"
    "Z2lzdGVyQ29ubmVjdGlvbihzZWxmKQogICAgICAgICNTdG9wIHRoZSB3cml0ZXIgZmlyc3Q6IGl0"
    "IGhvbGRzIHRoaXMgc29ja2V0IGFuZCB3b3VsZCBvdGhlcndpc2Uga2VlcAogICAgICAgICN3cml0"
    "aW5nIG9uIGJlaGFsZiBvZiBhIHBsYXllciB3aG8gaGFzIGFscmVhZHkgbGVmdCBldmVyeSBjaGFu"
    "bmVsLgogICAgICAgIHNlbGYuX3N0b3BXcml0ZXIoKQogICAgICAgIGlmIHNlbGYudXNlcjoKICAg"
    "ICAgICAgICAgcHJpbnQoZidVc2VyOiB7c2VsZi51c2VyLm5hbWV9IERpc2Nvbm5lY3RlZCcpCiAg"
    "ICAgICAgICAgIHNlbGYudXNlci5kaXNjb25uZWN0KHNlbGYuc2VydmVyKQogICAgICAgICNjbGVh"
    "bnVwIHVzZXIgZGF0YQogICAgICAgICNUT0RPIGNoZWNrIGlmIHRyaWdnZXJlZCBvbiBjcmFzaGVk"
    "IGNvbm5lY3Rpb24KICAgIGRlZiBkZWJ1Z19kaWN0KHNlbGYpOgogICAgICAgIGlmIHNlbGYudXNl"
    "ciBpcyBOb25lOgogICAgICAgICAgICAjUG9sbGVkIGJ5IHRoZSBjb250cm9sIHBhbmVsIG9uY2Ug"
    "YSBzZWNvbmQgd2hpbGUgcGxheWVycyBjb25uZWN0IGFuZAogICAgICAgICAgICAjZGlzY29ubmVj"
    "dDsgYSBjb25uZWN0aW9uIGNhdWdodCBiZXR3ZWVuIHRoZSB0d28gdXNlZCB0byByYWlzZSBoZXJl"
    "CiAgICAgICAgICAgICNhbmQgY29zdCB0aGUgcGFuZWwgaXRzIHdob2xlIHBsYXllciB0YWJsZSBm"
    "b3IgdGhhdCB0aWNrLgogICAgICAgICAgICByZXR1cm4geydnYW1lJzonJywgJ3Rvd24nOicnLCAn"
    "cG9zJzonJywgJ2lkJzowLCAnbG9naW5UaW1lJzonJ30KICAgICAgICByZXR1cm4gewogICAgICAg"
    "ICAgICAjVE9ETyBJUCBmb3IgZWxldmF0ZWQgYXV0aG9yaXR5CiAgICAgICAgICAgICMnbmFtZSc6"
    "c2VsZi51c2VyLm5hbWUsCiAgICAgICAgICAgICdnYW1lJzpzZWxmLnVzZXIuZ2FtZS5nbmFtZSBp"
    "ZiBzZWxmLnVzZXIuZ2FtZSBlbHNlICcnLAogICAgICAgICAgICAndG93bic6c2VsZi51c2VyLmdh"
    "bWVjaGFubmVsLm5hbWUgaWYgc2VsZi51c2VyLmdhbWVjaGFubmVsIGVsc2UgJycsCiAgICAgICAg"
    "ICAgICdwb3MnOnNlbGYudXNlci5wb3NkYXRhIGlmIHNlbGYudXNlci5wb3NkYXRhIGVsc2UgJycs"
    "CiAgICAgICAgICAgICdpZCc6c2VsZi51c2VyLmlkbnVtLAogICAgICAgICAgICAnbG9naW5UaW1l"
    "Jzpqc29uVGltZShzZWxmLnVzZXIubG9naW5UaW1lKQogICAgICAgIH0jVE9ETyBlbGV2YXRlZCBh"
    "dXRob3JpdHkgdmVyc2lvbgoKZGVmIGNtZF9kZWZhdWx0KCk6I2FyZ3MpOgogICAgI3ByaW50KGFy"
    "Z3MpCiAgICAjX3JlYWRjb25maWcoKQogICAgc2VydmVyID0gQ29yZVNlcnZlcigpCiAgICB3aXRo"
    "IHNlcnZlcjoKICAgICAgICB0c3QgPSBzaWduYWwuc2lnbmFsKHNpZ25hbC5TSUdJTlQsIHNlcnZl"
    "ci5oYW5kbGVfc2lnbmFsKHRpbWVvdXQ9MikpCiAgICAgICAgI3ByaW50KCdBc3NpZ25lZCBTaWdu"
    "YWw/JywgdHN0KQogICAgICAgICNzaWduYWwuc2lnbmFsKHNpZ25hbC5TSUdURVJNLCBzZXJ2ZXIu"
    "aGFuZGxlX3NpZ25hbCh0aW1lb3V0PTEpKQogICAgICAgIHNlcnZlci5zZXJ2ZV9mb3JldmVyKCkK"
    "CiNzY3JpcHQgbGF1bmNoZWQsIGNoZWNrIGFyZ3VtZW50cyBhbmQgY29uZmlnLiBzZXR1cCB2YXJp"
    "b3VzIG9iamVjdHMKaWYgX19uYW1lX18gPT0gJ19fbWFpbl9fJzoKICAgIHByaW50KCdJbml0aWFs"
    "aXppbmcgU2VydmVyJykKICAgIGNtZF9kZWZhdWx0KCkK"
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
