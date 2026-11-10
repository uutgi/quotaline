#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quotaline: a tiny balance and quota counter for Claude Code and OpenRouter.

One file, standard library only (Python 3.8+), identical on Windows, Linux and macOS.
The tray / menu-bar mode is the only one that needs extra packages (pystray, pillow).

Modes:
  quotaline.py                        one-shot report
  quotaline.py --watch [SECONDS]      refreshing counter for a small terminal window (default 60 s)
  quotaline.py --widget               small always-on-top window (tkinter, no extra packages)
  quotaline.py --tray                 system tray / menu-bar icon (needs: pip install pystray pillow)
  quotaline.py --statusline           Claude Code status line (reads Claude Code's JSON on stdin)
  quotaline.py --json                 the same data as JSON
  quotaline.py --days [N]             OpenRouter spend per day and model, last N completed days (30)
  quotaline.py --install-statusline   point the statusLine of ~/.claude/settings.json at this script
  quotaline.py --uninstall-statusline

Options:
  --show both|claude|openrouter   what to display (default: the saved choice, else both)
  --save                          save --show as the default in ~/.config/quotaline/config.json
  The same choice can be changed on the fly in the widget (options bar, right-click > Show,
  or keys 1 / 2 / 3) and in the tray menu (Show); the change is saved as the new default.
  --interval SECONDS              refresh period for --watch / --widget / --tray (default 60)
  --no-color

Data sources:
  OpenRouter: GET https://openrouter.ai/api/v1/key with a normal API key. Returns the key's
    spending limit, what is left of it, spend for today / this week / this month, and today's
    requests to :free models. The account-wide balance (GET /api/v1/credits) needs a separate
    "management key"; it is shown too when openrouter_management_key.txt exists. The same key
    unlocks --days: GET /api/v1/activity, spend per model for the last 30 completed UTC days.
  Claude Code: the 5-hour and 7-day plan limits are handed to the status line by Claude Code
    itself (`rate_limits`, documented, v2.1.251+). This script caches them in
    ~/.cache/quotaline/claude.json so the other modes can show them outside Claude Code, together
    with their age. No credentials are read and no undocumented endpoint is called.

OpenRouter key lookup order: OPENROUTER_API_KEY, openrouter_key.txt next to this script,
openrouter_key.txt in the parent folder, OpenCode's auth.json. The key is never printed.
"""
import argparse
import datetime
import json
import os
import queue
import random
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
HOME = os.path.expanduser('~')
CACHE_DIR = os.path.join(HOME, '.cache', 'quotaline')
CACHE_OPENROUTER = os.path.join(CACHE_DIR, 'openrouter.json')
CACHE_CLAUDE = os.path.join(CACHE_DIR, 'claude.json')
CONFIG_FILE = os.path.join(HOME, '.config', 'quotaline', 'config.json')
CLAUDE_SETTINGS = os.path.join(HOME, '.claude', 'settings.json')

OPENROUTER_KEY_URL = 'https://openrouter.ai/api/v1/key'
OPENROUTER_CREDITS_URL = 'https://openrouter.ai/api/v1/credits'
OPENROUTER_ACTIVITY_URL = 'https://openrouter.ai/api/v1/activity'
ACTIVITY_DAYS = 30            # the activity endpoint covers the last 30 completed UTC days
OPENROUTER_MAX_AGE = 300      # s: the status line asks OpenRouter at most once every 5 minutes
TIMEOUT_STATUSLINE = 2.5
TIMEOUT_NORMAL = 8

SHOW_CHOICES = ('both', 'claude', 'openrouter')
SHOW_LABELS = (('both', 'Both'), ('claude', 'Claude'), ('openrouter', 'OpenRouter'))
WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
IS_WINDOWS = os.name == 'nt'
IS_MAC = sys.platform == 'darwin'


# ---------------------------------------------------------------- helpers

def console_utf8():
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError):
            pass
    if IS_WINDOWS:
        os.system('')  # enables ANSI escape sequences in the classic console


def read_text(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except OSError:
        return None


def read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = read_json(CONFIG_FILE)
    return cfg if isinstance(cfg, dict) else {}


def save_config(**values):
    cfg = load_config()
    cfg.update(values)
    write_json(CONFIG_FILE, cfg)


def usd(x, decimals=2):
    if x is None:
        return '?'
    return '$' + ('{:.%df}' % decimals).format(float(x))


def pct(x):
    return '{:.0f}%'.format(float(x))


def clock(epoch):
    """HH:MM if today; 'Thu 09:00' otherwise (local time)."""
    if epoch is None:
        return '?'
    try:
        t = datetime.datetime.fromtimestamp(float(epoch))
    except (ValueError, OverflowError, OSError):
        return '?'
    if t.date() == datetime.date.today():
        return t.strftime('%H:%M')
    return WEEKDAYS[t.weekday()] + ' ' + t.strftime('%H:%M')


def until(epoch, compact=False):
    """Time left until an epoch: '2h 37m' / '3d 4h' / '12m' (compact: no spaces)."""
    if epoch is None:
        return '?'
    s = max(0, int(float(epoch) - time.time()))
    d, rest = divmod(s, 86400)
    h, rest = divmod(rest, 3600)
    m = rest // 60
    if d:
        parts = ['{}d'.format(d), '{}h'.format(h)]
    elif h:
        parts = ['{}h'.format(h), '{}m'.format(m)]
    else:
        parts = ['{}m'.format(m)]
    return ('' if compact else ' ').join(parts)


def ago(ts):
    if ts is None:
        return 'never'
    s = max(0, int(time.time() - ts))
    if s < 60:
        return '{} s ago'.format(s)
    if s < 3600:
        return '{} min ago'.format(s // 60)
    if s < 86400:
        return '{} h ago'.format(s // 3600)
    return '{} d ago'.format(s // 86400)


def level_of_used(used_pct):
    """'red' from 80 % used, 'yellow' from 50 %, else 'green'."""
    if used_pct is None:
        return 'green'
    return 'red' if used_pct >= 80 else 'yellow' if used_pct >= 50 else 'green'


def level_of_left(left, limit):
    if left is None:
        return 'green'
    if limit:
        return level_of_used(100 * (1 - left / limit))
    return 'red' if left < 2 else 'yellow' if left < 5 else 'green'


LEVEL_ORDER = {'green': 0, 'yellow': 1, 'red': 2}
LEVEL_RGB = {'green': (72, 180, 97), 'yellow': (222, 170, 40), 'red': (214, 64, 64)}

# Continuous green -> yellow -> red gradient by percentage used, anchored so that 50 % is a clear
# yellow and 80 % an unmistakable red (the same thresholds as the three-step fallback).
GRADIENT = [(0, (60, 180, 90)), (50, (232, 200, 50)), (80, (214, 64, 64)), (100, (190, 30, 30))]


def gradient_rgb(used_pct):
    u = min(100.0, max(0.0, float(used_pct if used_pct is not None else 0)))
    for (p0, c0), (p1, c1) in zip(GRADIENT, GRADIENT[1:]):
        if u <= p1:
            f = (u - p0) / (p1 - p0)
            return tuple(round(c0[i] + (c1[i] - c0[i]) * f) for i in range(3))
    return GRADIENT[-1][1]


def used_from_left(left, limit):
    """Percentage used for a 'what is left' figure: against the limit when there is one."""
    if left is None:
        return 0.0
    if limit:
        return 100.0 * (1 - float(left) / float(limit))
    return 95.0 if left < 2 else 65.0 if left < 5 else 10.0


def truecolor_terminal():
    override = os.environ.get('QUOTALINE_TRUECOLOR')
    if override in ('0', '1'):
        return override == '1'
    if os.environ.get('COLORTERM', '').lower() in ('truecolor', '24bit') or os.environ.get('WT_SESSION'):
        return True
    return IS_WINDOWS   # Windows 10+ consoles render 24-bit colour; other terminals get the 3 steps


class Color:
    """ANSI colours only when the output understands them (a terminal) or when asked (status line)."""
    enabled = False
    CODES = {'red': '\033[31m', 'yellow': '\033[33m', 'green': '\033[32m'}
    DIM, OFF = '\033[2m', '\033[0m'

    @classmethod
    def paint(cls, text, code):
        return code + text + cls.OFF if cls.enabled else text

    @classmethod
    def level(cls, text, level):
        return cls.paint(text, cls.CODES[level])

    @classmethod
    def used(cls, text, used_pct):
        """Colour by percentage used: a gradient where the terminal can show it, else 3 steps."""
        if not cls.enabled:
            return text
        if truecolor_terminal():
            return cls.paint(text, '\033[38;2;%d;%d;%dm' % gradient_rgb(used_pct))
        return cls.level(text, level_of_used(used_pct))

    @classmethod
    def left(cls, text, left, limit):
        return cls.used(text, used_from_left(left, limit))


GUI_MODE = False   # widget / tray: text goes to a Tk label or a menu, which render the block glyphs


def bar(fraction, width=10):
    fraction = min(1.0, max(0.0, fraction))
    n = int(round(fraction * width))
    full, empty = '█', '░'
    if not GUI_MODE:
        try:
            full.encode(sys.stdout.encoding or 'utf-8')
        except (UnicodeEncodeError, LookupError, AttributeError):
            full, empty = '#', '.'
    return full * n + empty * (width - n)


# ---------------------------------------------------------------- OpenRouter

def opencode_auth_paths():
    paths = []
    xdg = os.environ.get('XDG_DATA_HOME')
    if xdg:
        paths.append(os.path.join(xdg, 'opencode', 'auth.json'))
    paths.append(os.path.join(HOME, '.local', 'share', 'opencode', 'auth.json'))
    return paths


def key_from_file(path):
    text = read_text(path)
    if text:
        for token in text.split():
            if token.startswith('sk-or-'):
                return token
    return None


def openrouter_key():
    """(key, where it came from). The key itself is never printed."""
    k = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if k:
        return k, 'OPENROUTER_API_KEY'
    for folder in (HERE, PARENT):
        k = key_from_file(os.path.join(folder, 'openrouter_key.txt'))
        if k:
            return k, 'openrouter_key.txt'
    for path in opencode_auth_paths():
        d = read_json(path)
        if isinstance(d, dict):
            k = (d.get('openrouter') or {}).get('key')
            if k:
                return k, 'OpenCode auth.json'
    return None, None


def openrouter_management_key():
    for folder in (HERE, PARENT):
        k = key_from_file(os.path.join(folder, 'openrouter_management_key.txt'))
        if k:
            return k
    return None


_SSL = {}


def ssl_context():
    """Default context; if this Python ships no CA store (python.org builds on macOS before
    'Install Certificates.command'), fall back to certifi's bundle when it is installed."""
    if 'ctx' not in _SSL:
        ctx = ssl.create_default_context()
        if ctx.cert_store_stats().get('x509_ca', 0) == 0:
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                pass
        _SSL['ctx'] = ctx
    return _SSL['ctx']


def http_json(url, token, timeout):
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token,
                                               'User-Agent': 'quotaline/1.1'})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
        return json.loads(r.read().decode('utf-8'))


def describe_error(e):
    if isinstance(e, urllib.error.HTTPError):
        return 'HTTP {}'.format(e.code)
    if isinstance(e, urllib.error.URLError):
        if 'CERTIFICATE_VERIFY_FAILED' in str(e.reason):
            return 'this Python has no CA certificates (pip install certifi, or run Install Certificates.command)'
        return 'no network ({})'.format(getattr(e.reason, 'strerror', None) or e.reason)
    return type(e).__name__


def fetch_openrouter(timeout):
    key, source = openrouter_key()
    out = {'ts': time.time(), 'key_source': source}
    if not key:
        out['error'] = 'OpenRouter key not found'
        return out
    try:
        d = http_json(OPENROUTER_KEY_URL, key, timeout).get('data') or {}
        fields = ('label', 'limit', 'limit_remaining', 'limit_reset', 'usage', 'usage_daily',
                  'usage_weekly', 'usage_monthly', 'is_free_tier', 'free_model_daily_requests')
        out['key'] = {f: d.get(f) for f in fields}
    except Exception as e:  # noqa: BLE001 - any failure is shown, never breaks the status line
        out['error'] = describe_error(e)
    mk = openrouter_management_key()
    if mk:
        try:
            c = http_json(OPENROUTER_CREDITS_URL, mk, timeout).get('data') or {}
            bought, used = c.get('total_credits'), c.get('total_usage')
            out['account'] = {'total_credits': bought, 'total_usage': used,
                              'left': (bought - used) if bought is not None and used is not None else None}
        except Exception as e:  # noqa: BLE001
            out['account_error'] = describe_error(e)
    return out


def fetch_openrouter_days(timeout):
    """Spend per UTC day and model for the whole account (all keys), from GET /api/v1/activity.
    Needs the management key. Returns {'days': [newest first]} or {'error': text}."""
    mk = openrouter_management_key()
    if not mk:
        return {'error': 'needs the management key (openrouter_management_key.txt)'}
    try:
        rows = http_json(OPENROUTER_ACTIVITY_URL, mk, timeout).get('data') or []
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {'error': 'HTTP {}: openrouter_management_key.txt does not hold a valid management key'.format(e.code)}
        return {'error': describe_error(e)}
    except Exception as e:  # noqa: BLE001
        return {'error': describe_error(e)}
    days = {}
    for r in rows:
        date = str(r.get('date') or '?')[:10]           # 'YYYY-MM-DD' (sometimes with a time after it)
        usage, requests = float(r.get('usage') or 0), int(r.get('requests') or 0)
        d = days.setdefault(date, {'date': date, 'usage': 0.0, 'requests': 0, 'models': {}})
        m = d['models'].setdefault(r.get('model') or '?', {'usage': 0.0, 'requests': 0})
        for target in (d, m):
            target['usage'] += usage
            target['requests'] += requests
    return {'days': sorted(days.values(), key=lambda d: d['date'], reverse=True)}


def load_openrouter(max_age, timeout):
    """Cache for max_age seconds; if the network fails, return the last known data, flagged."""
    cached = read_json(CACHE_OPENROUTER)
    if cached and max_age and time.time() - cached.get('ts', 0) < max_age:
        return cached
    fresh = fetch_openrouter(timeout)
    if 'key' in fresh or not cached:
        write_json(CACHE_OPENROUTER, fresh)
        return fresh
    cached['stale'] = fresh.get('error', 'error')
    return cached


def openrouter_level(o):
    k = (o or {}).get('key')
    if not k:
        return 'red' if o else 'green'
    return level_of_left(k.get('limit_remaining'), k.get('limit'))


# ---------------------------------------------------------------- Claude Code

def save_claude_limits(statusline_input):
    rl = statusline_input.get('rate_limits')
    if not rl:
        return
    model = (statusline_input.get('model') or {}).get('display_name')
    write_json(CACHE_CLAUDE, {'ts': time.time(), 'rate_limits': rl, 'model': model,
                              'version': statusline_input.get('version')})


def claude_windows(rate_limits):
    """[(label, used %, resets_at epoch)] for the windows that are present."""
    out = []
    for field, label in (('five_hour', '5h'), ('seven_day', '7d')):
        w = (rate_limits or {}).get(field) or {}
        if w.get('used_percentage') is not None:
            out.append((label, float(w['used_percentage']), w.get('resets_at')))
    return out


def claude_level(cl):
    levels = [level_of_used(used) for _, used, _ in claude_windows((cl or {}).get('rate_limits'))]
    return max(levels, key=LEVEL_ORDER.get) if levels else 'green'


# ---------------------------------------------------------------- data for the non-statusline modes

def collect(show, max_age=0, timeout=TIMEOUT_NORMAL):
    """(openrouter data or None, claude cache or None) according to `show`."""
    o = load_openrouter(max_age, timeout) if show != 'claude' else None
    cl = read_json(CACHE_CLAUDE) if show != 'openrouter' else None
    return o, cl


def worst_level(o, cl, show):
    levels = []
    if show != 'claude':
        levels.append(openrouter_level(o))
    if show != 'openrouter':
        levels.append(claude_level(cl))
    return max(levels, key=LEVEL_ORDER.get) if levels else 'green'


def worst_used(o, cl, show):
    """Highest percentage used among what is shown (drives the widget border and the tray dot)."""
    values = [0.0]
    if show != 'claude':
        k = (o or {}).get('key')
        if k:
            values.append(used_from_left(k.get('limit_remaining'), k.get('limit')))
        elif o:
            values.append(100.0)   # error: no data, flag it red
    if show != 'openrouter':
        values += [used for _, used, _ in claude_windows((cl or {}).get('rate_limits'))]
    return max(values)


# ---------------------------------------------------------------- output

def period(limit_reset):
    return {'daily': 'day', 'weekly': 'wk', 'monthly': 'mo'}.get(str(limit_reset).lower(), str(limit_reset))


def openrouter_short(o):
    k = o.get('key')
    if not k:
        return 'OpenRouter: ' + Color.level(o.get('error', '?'), 'red')
    limit, left = k.get('limit'), k.get('limit_remaining')
    if limit is not None and left is not None:
        text = '{} left of {}'.format(usd(left), usd(limit, 0))
        if k.get('limit_reset'):
            text += '/' + period(k['limit_reset'])
        text = 'OpenRouter: ' + Color.left(text, left, limit)
    else:
        text = 'OpenRouter: {} spent/mo'.format(usd(k.get('usage_monthly')))
    if o.get('account') and o['account'].get('left') is not None:
        text += Color.paint(', account ' + usd(o['account']['left']), Color.DIM)
    if o.get('stale'):
        text += Color.paint(' (' + ago(o.get('ts')) + ')', Color.DIM)
    return text


def claude_short(cl, header='Claude'):
    """'Claude: 5h 22% (13:10), 7d 35% (Thu 13:00)' from the cache (other modes than the status line)."""
    if not cl:
        return header + ': ' + Color.paint('no data yet', Color.DIM)
    parts = [Color.used(label + ' ' + pct(used), used) + Color.paint(' (' + clock(r) + ', ' + until(r, compact=True) + ')', Color.DIM)
             for label, used, r in claude_windows(cl.get('rate_limits'))]
    return header + ': ' + ', '.join(parts)


def statusline(d, show):
    """One line: 'Claude <model>: ctx, 5h, 7d  |  OpenRouter: ...'.

    Only ASCII separators: box-drawing and arrow glyphs render badly in some terminal fonts.
    """
    blocks = []
    if show != 'openrouter':
        model = (d.get('model') or {}).get('display_name')
        header = 'Claude ' + model if model else 'Claude'
        parts = []
        ctx = (d.get('context_window') or {}).get('used_percentage')
        if ctx is not None:
            parts.append(Color.used('ctx ' + pct(ctx), ctx))
        windows = claude_windows(d.get('rate_limits'))
        if windows:
            for label, used, resets_at in windows:
                parts.append(Color.used(label + ' ' + pct(used), used)
                             + Color.paint(' (' + clock(resets_at) + ', ' + until(resets_at, compact=True) + ')', Color.DIM))
        else:
            parts.append(Color.paint('limits after 1st reply', Color.DIM))
        blocks.append(header + ': ' + ', '.join(parts))
    if show != 'claude':
        blocks.append(openrouter_short(load_openrouter(OPENROUTER_MAX_AGE, TIMEOUT_STATUSLINE)))
    return '  |  '.join(blocks)


def report(o, cl, show):
    L = [Color.paint('quotaline', Color.DIM) + '  ' + datetime.datetime.now().strftime('%Y-%m-%d %H:%M')]
    if show != 'claude':
        k = (o or {}).get('key')
        title = 'OpenRouter'
        # a key without a name comes back with the masked key as its label: not shown
        if k and k.get('label') and not str(k['label']).startswith('sk-or-'):
            title += '  key "{}"'.format(k['label'])
        if o and o.get('stale'):
            title += Color.level('  (offline: data from {})'.format(ago(o.get('ts'))), 'yellow')
        L.append(title)
        if not k:
            L.append('  ' + Color.level((o or {}).get('error', 'no data'), 'red'))
        else:
            limit, left = k.get('limit'), k.get('limit_remaining')
            if limit is not None and left is not None:
                fraction = left / limit if limit else 0
                text = usd(left) + ' of ' + usd(limit) + ('/' + period(k['limit_reset']) if k.get('limit_reset') else '')
                L.append('  Left:   {}  {} {}'.format(Color.left(text, left, limit),
                                                      bar(fraction), pct(100 * fraction)))
            else:
                L.append('  Key has no spending limit')
            L.append('  Spent:  today {}, week {}, month {}, all time {}'.format(
                usd(k.get('usage_daily')), usd(k.get('usage_weekly')), usd(k.get('usage_monthly')), usd(k.get('usage'))))
            free = k.get('free_model_daily_requests') or {}
            if free.get('limit') is not None and (free.get('used') or 0) > 0:
                # the daily quota of requests to :free models; only worth a line once it is being used
                L.append('  :free models today: {} of {} requests'.format(free.get('used'), free.get('limit')))
        if o and o.get('account'):
            a = o['account']
            L.append('  Account: {} left (bought {}, used {})'.format(
                usd(a.get('left')), usd(a.get('total_credits')), usd(a.get('total_usage'))))
        elif o and o.get('account_error'):
            L.append('  Account: ' + Color.level(o['account_error'], 'yellow'))
    if show == 'both':
        L.append('')
    if show != 'openrouter':
        if not cl:
            L.append('Claude Code  ' + Color.paint('(no data yet: filled in by the status line)', Color.DIM))
        else:
            L.append('Claude Code' + (' ' + cl['model'] if cl.get('model') else '')
                     + Color.paint('  (last update {})'.format(ago(cl.get('ts'))), Color.DIM))
            for label, used, resets_at in claude_windows(cl.get('rate_limits')):
                L.append('  {}:  {}  {} used, resets {}'.format(
                    label, bar(used / 100), Color.used(pct(used), used), clock(resets_at))
                         + Color.paint(' (in {})'.format(until(resets_at)), Color.DIM))
    return '\n'.join(L)


def days_report(res, n_days):
    """OpenRouter spend per day for the last n_days completed UTC days, each day split by model."""
    L = ['OpenRouter spend per day' + Color.paint(
        '  (UTC days, all keys of the account; today is not included)', Color.DIM)]
    if 'error' in res:
        L.append('  ' + Color.level(res['error'], 'red'))
        return '\n'.join(L)
    cutoff = (datetime.datetime.now(datetime.timezone.utc).date()
              - datetime.timedelta(days=n_days)).isoformat()
    days = [d for d in res['days'] if d['date'] >= cutoff]
    if not days:
        L.append('  no activity in the last {} days'.format(n_days))
        return '\n'.join(L)
    top = max(d['usage'] for d in days) or 1.0
    for d in days:
        try:
            label = WEEKDAYS[datetime.date.fromisoformat(d['date']).weekday()] + ' ' + d['date']
        except ValueError:
            label = d['date']
        L.append('  {:14}  {:>8}  {:>5} req  {}'.format(
            label, usd(d['usage'], 4), d['requests'], bar(d['usage'] / top)))
        for model, m in sorted(d['models'].items(), key=lambda kv: -kv[1]['usage']):
            L.append(Color.paint('  {:14}  {:>8}  {:>5} req  {}'.format(
                '', usd(m['usage'], 4), m['requests'], model), Color.DIM))
    L.append('  {:14}  {:>8}  {:>5} req  active days: {} of {}'.format(
        'Total', usd(sum(d['usage'] for d in days), 4), sum(d['requests'] for d in days),
        len(days), n_days))
    return '\n'.join(L)


def summary(o, cl, show):
    """Short text for tooltips and the widget title, no colours."""
    was = Color.enabled
    Color.enabled = False
    try:
        blocks = []
        if show != 'openrouter':
            blocks.append(claude_short(cl))
        if show != 'claude':
            blocks.append(openrouter_short(o) if o else 'OpenRouter: ?')
        return '  |  '.join(blocks)
    finally:
        Color.enabled = was


def as_json(o, cl):
    return json.dumps({'openrouter': o, 'claude': cl}, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- widget (tkinter, stdlib)

def point_on_screen(root, x, y):
    """True when (x, y) lies on a connected monitor."""
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes
            monitor_from_point = ctypes.windll.user32.MonitorFromPoint
            monitor_from_point.argtypes = [wintypes.POINT, wintypes.DWORD]
            monitor_from_point.restype = ctypes.c_void_p
            return bool(monitor_from_point(wintypes.POINT(x, y), 0))   # 0 = MONITOR_DEFAULTTONULL
        except (AttributeError, OSError):
            pass
    elif IS_MAC:
        return True     # the widget keeps its title bar there, and macOS keeps title bars on screen
    # X11: the screen spans every monitor (on Windows, only the primary one: a fallback)
    return 0 <= x < root.winfo_screenwidth() and 0 <= y < root.winfo_screenheight()


def run_widget(show, interval, opacity, font_size=None):
    try:
        import tkinter as tk
    except ImportError:
        sys.exit('tkinter is missing. Debian/Ubuntu/Mint: sudo apt install python3-tk')
    global GUI_MODE
    GUI_MODE, Color.enabled = True, True            # the report is rendered with colour tags
    os.environ['QUOTALINE_TRUECOLOR'] = '1'          # always the gradient inside the widget
    root = tk.Tk()
    root.title('quotaline')
    if IS_MAC:
        # Tk on macOS does not deliver mouse clicks to override-redirect windows, so keep the
        # native title bar there: it gives the red close button and dragging for free.
        root.resizable(False, False)
    else:
        root.overrideredirect(True)        # no title bar: a small floating panel
    root.attributes('-topmost', True)
    def set_opacity(value, save=False):
        value = min(1.0, max(0.2, float(value)))
        try:
            root.attributes('-alpha', value)
        except tk.TclError:
            pass
        if save:
            save_config(widget_opacity=value)
        return value

    opacity = set_opacity(opacity)
    cfg = load_config()
    pos = cfg.get('widget_pos')
    if isinstance(pos, list) and len(pos) == 2:
        x, y = int(pos[0]), int(pos[1])
        # The saved spot may belong to a monitor that is not connected any more: then let the
        # system place the window (as on the first run) instead of opening it off-screen.
        if point_on_screen(root, x + 20, y + 10):
            root.geometry('+{}+{}'.format(x, y))
    import tkinter.font as tkfont
    family, default_size = ('Consolas', 10) if IS_WINDOWS else ('Menlo', 10) if IS_MAC else ('DejaVu Sans Mono', 9)
    size = int(font_size or load_config().get('widget_font_size') or default_size)
    size = min(24, max(7, size))
    font = (family, size)
    text_font = tkfont.Font(family=family, size=size)

    def set_font_size(new_size, save=False):
        new_size = min(24, max(7, int(new_size)))
        text_font.configure(size=new_size)
        if save:
            save_config(widget_font_size=new_size)
        if options.winfo_ismapped():      # the options row is open: re-wrap it for the new width
            root.after(50, layout_options)
        return new_size
    frame = tk.Frame(root, bg='#1c1c28', highlightthickness=2, highlightbackground='#48b461')
    frame.pack(fill='both', expand=True)
    label = tk.Text(frame, font=text_font, bg='#1c1c28', fg='#e6e6e6', bd=0, highlightthickness=0,
                    padx=10, pady=7, wrap='none', cursor='arrow', takefocus=0, height=3, width=40)
    label.tag_configure('dim', foreground='#8a8a9a')
    label.insert('1.0', 'quotaline...')
    label.config(state='disabled')
    label.pack(fill='both', expand=True)
    ansi_re = re.compile('\033\\[([0-9;]*)m')
    ansi_basic = {'31': '#d64040', '32': '#48b461', '33': '#deaa28'}

    def render(widget, coloured):
        """Write an ANSI-coloured string into the Text widget as colour tags (same output as the terminal)."""
        widget.config(state='normal')
        widget.delete('1.0', 'end')
        pos, tag = 0, None
        for m in ansi_re.finditer(coloured):
            if m.start() > pos:
                widget.insert('end', coloured[pos:m.start()], tag or ())
            code = m.group(1)
            if code in ('', '0'):
                tag = None
            elif code == '2':
                tag = 'dim'
            elif code.startswith('38;2;'):
                r, g, b = (int(x) for x in code.split(';')[2:5])
                tag = 'c%02x%02x%02x' % (r, g, b)
                widget.tag_configure(tag, foreground='#' + tag[1:])
            elif code in ansi_basic:
                tag = 'b' + code
                widget.tag_configure(tag, foreground=ansi_basic[code])
            pos = m.end()
        widget.insert('end', coloured[pos:], tag or ())
        plain = ansi_re.sub('', coloured).split('\n')
        widget.config(height=len(plain), width=max(len(line) for line in plain) + 1, state='disabled')
    # close button in the top-right corner (the panel has no title bar); Escape closes too
    hand = '' if IS_MAC else 'hand2'   # macOS users expect the normal pointer; the buttons are big enough
    close = tk.Label(frame, text='×', font=(font[0], font[1] + 5, 'bold'), bg='#1c1c28', fg='#8a8a9a',
                     padx=9, pady=1, cursor=hand)
    close.place(relx=1.0, x=-2, y=0, anchor='ne')
    close.bind('<Button-1>', lambda _e: root.destroy())
    close.bind('<Enter>', lambda _e: close.config(fg='#ff6b6b'))
    close.bind('<Leave>', lambda _e: close.config(fg='#8a8a9a'))
    root.bind('<Escape>', lambda _e: root.destroy())

    drag = {'x': 0, 'y': 0}

    def press(e):
        drag['x'], drag['y'] = e.x, e.y
        if options.winfo_ismapped():
            options.pack_forget()
        return 'break'

    def move(e):
        root.geometry('+{}+{}'.format(e.x_root - drag['x'], e.y_root - drag['y']))
        return 'break'

    def release(_e):
        save_config(widget_pos=[root.winfo_x(), root.winfo_y()])
        return 'break'

    # In-panel options bar, toggled by the menu button (and by right-click on macOS, where
    # Tk's popup menus leave the window deaf afterwards). Elsewhere right-click gets a real popup.
    options = tk.Frame(frame, bg='#1c1c28')
    option_specs = []        # [(caption or None, [(text, command, key)])]: one captioned row per group
    option_widgets = {}      # key -> button widget of the current layout (rebuilt on every open)

    def option_group(caption):
        option_specs.append((caption, []))

    def option_button(text, command, key=None):
        option_specs[-1][1].append((text, command, key))

    def make_button(parent, text, command):
        b = tk.Label(parent, text=text, font=font, bg='#2b2b3d', fg='#e6e6e6', padx=8, pady=3, cursor=hand)
        b.bind('<Button-1>', lambda _e: command())   # the row stays open; a click elsewhere on the panel closes it
        b.bind('<Enter>', lambda _e: b.config(bg='#3b3b52'))
        b.bind('<Leave>', lambda _e: b.config(bg='#2b2b3d'))
        return b

    def layout_options():
        # Rebuilt every time the panel opens: one row per group with a dim caption at a fixed
        # width ("Show", "Opacity", "Text"); each row packs its buttons left to right and wraps
        # onto a continuation row when they would exceed the panel's current width. Opening the
        # options therefore never makes the widget wider than its text.
        root.update_idletasks()
        avail = max(label.winfo_width() - 4, 160)
        for child in options.winfo_children():
            child.destroy()
        option_widgets.clear()
        capw = max([tkfont.Font(font=font).measure(c) for c, _ in option_specs if c] + [0]) + 12
        rowh = make_button(options, 'X', lambda: None)
        bh = rowh.winfo_reqheight(); rowh.destroy()

        def new_row(caption):
            row = tk.Frame(options, bg='#1c1c28'); row.pack(fill='x', pady=(0, 4))
            cell = tk.Frame(row, bg='#1c1c28', width=capw, height=bh); cell.pack(side='left')
            cell.pack_propagate(False)
            if caption:
                tk.Label(cell, text=caption, font=font, bg='#1c1c28', fg='#8a8a9a', anchor='w').pack(side='left', fill='y')
            return row

        for caption, items in option_specs:
            row, used = new_row(caption), capw
            for text, command, key in items:
                b = make_button(row, text, command)
                w = b.winfo_reqwidth() + 4
                if used > capw and used + w > avail:        # continuation row, caption left blank
                    b.destroy()
                    row, used = new_row(None), capw
                    b = make_button(row, text, command)
                b.pack(side='left', padx=(0, 4))
                used += w
                if key:
                    option_widgets[key] = b
        paint_show_buttons()

    def toggle_options(_e=None):
        if options.winfo_ismapped():
            options.pack_forget()
        else:
            layout_options()
            options.pack(fill='x', padx=8, pady=(0, 2))

    # What is shown (both / Claude / OpenRouter): changeable from the panel, saved as the new default.
    view = {'show': show}

    def paint_show_buttons():
        for key, _text in SHOW_LABELS:
            b = option_widgets.get('show:' + key)
            if b is None:
                continue
            on = key == view['show']
            b.config(bg='#3b4f7a' if on else '#2b2b3d')
            b.bind('<Leave>', lambda _e, b=b, on=on: b.config(bg='#3b4f7a' if on else '#2b2b3d'))

    def set_show(value):
        if value not in SHOW_CHOICES or value == view['show']:
            return
        view['show'] = value
        save_config(show=value)
        show_var.set(value)
        paint_show_buttons()
        refresh()

    option_group('Show')
    for key, text in SHOW_LABELS:
        option_button(text, lambda v=key: set_show(v), key='show:' + key)
    option_group('Opacity')
    for level_pct in (100, 90, 80, 70, 60, 50, 40):
        option_button('{}%'.format(level_pct), lambda v=level_pct / 100: set_opacity(v, save=True))
    option_group('Text')
    option_button('A-', lambda: set_font_size(text_font.cget('size') - 1, save=True))
    option_button('A+', lambda: set_font_size(text_font.cget('size') + 1, save=True))
    option_group(None)
    option_button('Refresh', lambda: refresh())
    option_button('Quit', root.destroy)

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label='Refresh', command=lambda: refresh())
    show_menu = tk.Menu(menu, tearoff=0)
    show_var = tk.StringVar(value=show)
    for key, text in SHOW_LABELS:
        show_menu.add_radiobutton(label=text, value=key, variable=show_var, command=lambda v=key: set_show(v))
    menu.add_cascade(label='Show', menu=show_menu)
    opacity_menu = tk.Menu(menu, tearoff=0)
    for level_pct in (100, 90, 80, 70, 60, 50, 40):
        opacity_menu.add_command(label='{} %'.format(level_pct),
                                 command=lambda v=level_pct / 100: set_opacity(v, save=True))
    menu.add_cascade(label='Opacity', menu=opacity_menu)
    menu.add_separator()
    menu.add_command(label='Quit', command=root.destroy)

    def nudge_opacity(delta):
        current = float(root.attributes('-alpha') or 1.0)
        set_opacity(round(current + delta, 2), save=True)

    for key in ('<plus>', '<KP_Add>', '<equal>'):
        root.bind(key, lambda _e: nudge_opacity(+0.1))
    for key in ('<minus>', '<KP_Subtract>'):
        root.bind(key, lambda _e: nudge_opacity(-0.1))
    root.bind('<Control-plus>', lambda _e: set_font_size(text_font.cget('size') + 1, save=True))
    root.bind('<Control-equal>', lambda _e: set_font_size(text_font.cget('size') + 1, save=True))
    root.bind('<Control-minus>', lambda _e: set_font_size(text_font.cget('size') - 1, save=True))
    root.bind('<Key-1>', lambda _e: set_show('both'))          # 1 both, 2 Claude, 3 OpenRouter
    root.bind('<Key-2>', lambda _e: set_show('claude'))
    root.bind('<Key-3>', lambda _e: set_show('openrouter'))

    def popup(e):
        if IS_MAC:
            toggle_options()
            return
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    for w in (frame, label):
        w.bind('<ButtonPress-1>', press)
        w.bind('<B1-Motion>', move)
        w.bind('<ButtonRelease-1>', release)
        w.bind('<Button-3>', popup)
        w.bind('<Button-2>', popup)            # macOS two-finger / secondary click
        w.bind('<Control-Button-1>', popup)    # macOS Control-click (Tk does not map it to Button-2)
    # menu button next to the close button: toggles the options bar with a plain left click
    menu_btn = tk.Label(frame, text='≡', font=(font[0], font[1] + 5, 'bold'), bg='#1c1c28', fg='#8a8a9a',
                        padx=9, pady=1, cursor=hand)
    menu_btn.place(relx=1.0, x=-36, y=0, anchor='ne')
    menu_btn.bind('<Button-1>', toggle_options)
    menu_btn.bind('<Enter>', lambda _e: menu_btn.config(fg='#e6e6e6'))
    menu_btn.bind('<Leave>', lambda _e: menu_btn.config(fg='#8a8a9a'))
    # activity spinner, shown only while a refresh is in flight (like Claude Code's own spinner)
    spinner = tk.Label(frame, text='', font=font, bg='#1c1c28', fg='#8a8a9a')
    spinner.place(relx=1.0, x=-74, y=2, anchor='ne')
    spin = {'on': False, 'i': 0, 'frames': '\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f'}

    # whimsical status verbs while refreshing, in the spirit of Claude Code's own (ours are about counting)
    verbs = ('Tallying', 'Abacusing', 'Rummaging', 'Counting beans', 'Squinting at receipts', 'Auditing',
             'Ferreting', 'Pondering', 'Noodling', 'Totting up', 'Reckoning', 'Peeking', 'Sniffing quotas',
             'Shaking the piggy bank', 'Consulting the oracle', 'Dusting the ledger', 'Nudging the gauge')

    def spin_step():
        if not spin['on']:
            spinner.config(text='')
            return
        spin['i'] = (spin['i'] + 1) % len(spin['frames'])
        spinner.config(text=spin['frames'][spin['i']] + ' ' + spin['verb'] + '…')
        root.after(80, spin_step)

    def spin_start():
        spin['since'] = time.time()
        spin['verb'] = random.choice(verbs)
        if not spin['on']:
            spin['on'] = True
            spin_step()

    def spin_stop():
        # keep it visible at least 1.5 s: a 300 ms fetch would otherwise be an invisible blink
        remaining = 1.5 - (time.time() - spin.get('since', 0))
        if remaining > 0:
            root.after(int(remaining * 1000), spin_stop)
            return
        spin['on'] = False

    timer = {'id': None}
    results = queue.Queue()

    def apply(text, used):
        render(label, text)
        frame.config(highlightbackground='#%02x%02x%02x' % gradient_rgb(used))

    def refresh():
        # The worker thread only fetches data; Tk is touched from the main thread alone
        # (calling Tk from another thread breaks the event loop, visibly so on macOS).
        if timer['id'] is not None:
            root.after_cancel(timer['id'])
            timer['id'] = None

        def work():
            current = view['show']
            o, cl = collect(current)
            results.put((report(o, cl, current), worst_used(o, cl, current)))   # coloured: see Color.enabled below
        spin_start()
        threading.Thread(target=work, daemon=True).start()

    def poll():
        try:
            while True:
                text, used = results.get_nowait()
                spin_stop()
                apply(text, used)
                timer['id'] = root.after(interval * 1000, refresh)
        except queue.Empty:
            pass
        root.after(200, poll)

    poll()
    refresh()
    if IS_MAC:
        # macOS shows custom cursors and delivers clicks only to the active application, and a
        # process started from a terminal or a launcher is not active: activate ourselves.
        def activate():
            try:
                from AppKit import NSApplication
                NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            except Exception:  # noqa: BLE001 - pyobjc not installed: fall back to Tk's own lift
                pass
            root.lift()
            root.focus_force()
        root.after(300, activate)
    root.mainloop()


# ---------------------------------------------------------------- tray / menu bar (pystray, optional)

def run_tray(show, interval):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        msg = 'The tray needs two packages:  pip install pystray pillow'
        if not IS_WINDOWS and not IS_MAC:
            msg += '\nLinux also needs the desktop bindings, e.g. sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1'
        sys.exit(msg)
    global GUI_MODE
    GUI_MODE, Color.enabled = True, False

    def make_icon(used):
        s = 64
        img = Image.new('RGBA', (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([2, 2, s - 3, s - 3], fill=gradient_rgb(used) + (255,))
        # a small white gauge: arc open at the bottom plus the needle
        d.arc([14, 16, s - 15, s - 13], start=150, end=390, fill=(255, 255, 255, 255), width=7)
        d.line([(s / 2, s / 2 + 4), (s / 2 + 12, s / 2 - 8)], fill=(255, 255, 255, 255), width=6)
        return img

    state = {'o': None, 'cl': None, 'ts': 0.0, 'show': show}

    def gather(max_age=0):
        state['o'], state['cl'] = collect(state['show'], max_age=max_age)
        state['ts'] = time.time()

    def open_widget(_icon=None, _item=None):
        subprocess.Popen([sys.executable, os.path.abspath(__file__), '--widget', '--show', state['show'],
                          '--interval', str(interval)])

    def set_show(icon, value):
        if value == state['show']:
            return
        state['show'] = value
        save_config(show=value)
        refresh(icon)

    def show_action(value):
        # pystray rejects actions with more than two parameters, so no `v=key` default argument
        return lambda icon, _item: set_show(icon, value)

    def menu_items():
        # Built every time the menu opens, so it always shows the latest data; if the last
        # fetch is older than 15 s, refresh from the caches first (instant, no network wait).
        if time.time() - state['ts'] > 15:
            gather(max_age=60)
        for line in report(state['o'], state['cl'], state['show']).split('\n')[1:]:
            if line.strip():
                yield pystray.MenuItem(line.rstrip(), None, enabled=False)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem('Open widget', open_widget, default=True)
        yield pystray.MenuItem('Refresh', lambda icon, item: refresh(icon))
        yield pystray.MenuItem('Show', pystray.Menu(*[
            pystray.MenuItem(text, show_action(key), radio=True,
                             checked=lambda item, k=key: state['show'] == k)
            for key, text in SHOW_LABELS]))
        yield pystray.MenuItem('Quit', lambda icon, item: icon.stop())

    def apply_ui(icon, used, title):
        icon.icon = make_icon(used)
        icon.title = title
        try:
            icon.update_menu()
        except Exception:  # noqa: BLE001 - some backends have no dynamic menus
            pass

    def refresh(icon):
        gather()   # network and files: fine on any thread
        used = worst_used(state['o'], state['cl'], state['show'])
        title = summary(state['o'], state['cl'], state['show'])[:120]
        if IS_MAC:
            # AppKit must be touched from the main thread, or the process dies with EXC_BREAKPOINT;
            # the refresh loop runs in pystray's setup thread, so hand the UI update over.
            try:
                from PyObjCTools import AppHelper
                AppHelper.callAfter(apply_ui, icon, used, title)
                return
            except ImportError:
                pass
        apply_ui(icon, used, title)

    def loop(icon):
        icon.visible = True
        while icon.visible:
            try:
                refresh(icon)
            except Exception:  # noqa: BLE001 - keep the tray alive whatever happens
                pass
            time.sleep(interval)

    gather()
    icon = pystray.Icon('quotaline', make_icon(worst_used(state['o'], state['cl'], state['show'])),
                        summary(state['o'], state['cl'], state['show'])[:120], pystray.Menu(menu_items))
    icon.run(setup=loop)


# ---------------------------------------------------------------- install

def statusline_command(show):
    python = 'python' if IS_WINDOWS else 'python3'
    path = os.path.abspath(__file__).replace('\\', '/')
    cmd = '{} "{}" --statusline'.format(python, path)
    if show != 'both':
        cmd += ' --show ' + show
    return cmd


def install_statusline(show, remove=False):
    settings = read_json(CLAUDE_SETTINGS)
    if settings is None:
        if os.path.exists(CLAUDE_SETTINGS):
            sys.exit('Cannot read {} as JSON; nothing changed.'.format(CLAUDE_SETTINGS))
        settings = {}
    if os.path.exists(CLAUDE_SETTINGS):
        backup = CLAUDE_SETTINGS + '.bak-quotaline-' + time.strftime('%Y%m%d-%H%M%S')
        shutil.copy2(CLAUDE_SETTINGS, backup)
        print('Backup:', backup)
    if remove:
        if 'statusLine' in settings:
            del settings['statusLine']
            print('statusLine removed.')
        else:
            print('There was no statusLine.')
    else:
        settings['statusLine'] = {'type': 'command', 'command': statusline_command(show), 'refreshInterval': 30}
        print('statusLine ->', settings['statusLine']['command'])
    os.makedirs(os.path.dirname(CLAUDE_SETTINGS), exist_ok=True)
    with open(CLAUDE_SETTINGS, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print('Written', CLAUDE_SETTINGS, '(reopen Claude Code to see it).')


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description='Balance and quota counter for Claude Code and OpenRouter.')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--watch', nargs='?', const=True, metavar='SEC', help='refreshing counter in the terminal')
    g.add_argument('--widget', action='store_true', help='small always-on-top window (tkinter)')
    g.add_argument('--tray', action='store_true', help='system tray / menu-bar icon (needs pystray + pillow)')
    g.add_argument('--statusline', action='store_true', help='Claude Code status line mode (JSON on stdin)')
    g.add_argument('--json', action='store_true', help='JSON output')
    g.add_argument('--days', nargs='?', type=int, const=ACTIVITY_DAYS, metavar='N',
                   help='OpenRouter spend per day and model, last N completed UTC days '
                        '(default and maximum {}; needs the management key)'.format(ACTIVITY_DAYS))
    g.add_argument('--install-statusline', action='store_true', help='write statusLine into ~/.claude/settings.json')
    g.add_argument('--uninstall-statusline', action='store_true')
    ap.add_argument('--show', choices=SHOW_CHOICES, help='what to display (default: saved choice, else both)')
    ap.add_argument('--save', action='store_true', help='save --show as the default')
    ap.add_argument('--interval', type=int, default=60, metavar='SEC', help='refresh period (60)')
    ap.add_argument('--opacity', type=float, metavar='0.2-1', help='widget opacity (default: saved value, else 0.95)')
    ap.add_argument('--font-size', type=int, metavar='7-24', help='widget font size (default: saved value, else per platform)')
    ap.add_argument('--no-color', action='store_true')
    args = ap.parse_args()
    console_utf8()

    cfg = load_config()
    show = args.show or cfg.get('show') or 'both'
    if show not in SHOW_CHOICES:
        show = 'both'
    mode_given = any((args.watch is not None, args.widget, args.tray, args.statusline, args.json,
                      args.days is not None, args.install_statusline, args.uninstall_statusline))
    opacity = args.opacity if args.opacity is not None else cfg.get('widget_opacity', 0.95)
    if args.save:
        if not args.show and args.opacity is None and args.font_size is None:
            sys.exit('--save needs --show, --opacity and/or --font-size')
        saved = {}
        if args.show:
            saved['show'] = show
        if args.opacity is not None:
            saved['widget_opacity'] = min(1.0, max(0.2, args.opacity))
        if args.font_size is not None:
            saved['widget_font_size'] = min(24, max(7, args.font_size))
        save_config(**saved)
        print('Saved:', ', '.join('{} = {}'.format(k, v) for k, v in saved.items()), '->', CONFIG_FILE)
        if not mode_given:
            return
    interval = max(5, args.interval)
    if args.watch not in (None, True):      # --watch 30 (old form) still works
        try:
            interval = max(5, int(args.watch))
        except ValueError:
            sys.exit('--watch takes a number of seconds')

    if args.install_statusline:
        return install_statusline(show)
    if args.uninstall_statusline:
        return install_statusline(show, remove=True)

    if args.statusline:
        Color.enabled = not args.no_color
        try:
            d = json.loads(sys.stdin.read() or '{}')
        except ValueError:
            d = {}
        save_claude_limits(d)   # always: keeps the cache fresh even when only OpenRouter is shown
        print(statusline(d, show))
        return
    if args.widget:
        return run_widget(show, interval, opacity, args.font_size)
    if args.tray:
        return run_tray(show, interval)

    Color.enabled = sys.stdout.isatty() and not args.no_color
    if args.days is not None:
        print(days_report(fetch_openrouter_days(TIMEOUT_NORMAL), min(ACTIVITY_DAYS, max(1, args.days))))
        return
    if args.json:
        o, cl = collect(show)
        print(as_json(o, cl))
        return
    if args.watch is None:
        o, cl = collect(show)
        print(report(o, cl, show))
        return
    try:
        while True:
            o, cl = collect(show)
            sys.stdout.write('\033[2J\033[H' + report(o, cl, show) + '\n'
                             + Color.paint('\nrefreshes every {} s, Ctrl+C to quit'.format(interval), Color.DIM) + '\n')
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print()


if __name__ == '__main__':
    main()
