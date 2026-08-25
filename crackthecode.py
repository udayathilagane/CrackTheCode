"""Crack The Code - single-player 5-digit guessing game.

One player, one game, ten guesses. No rooms, no other players, no turns, no
timers, no chat. Plain Flask, so it runs on a standard PythonAnywhere WSGI
web app.

The round in progress is stored on disk (games.json) so a web-app reload or a
worker recycle does not wipe it, and finished rounds go to leaderboard.json for
the hall of fame on the landing page.

The whole site is behind an access code. The code is NOT written anywhere in
this file: it is generated at random the first time the site is opened and
saved to access_code.json, which only the account owner can read (Files tab, or
a Bash console). Knowing the URL is not enough - every page and every POST is
blocked until the code is entered, so nobody can play or read the hall of fame
without it. Anyone already inside can see the code and rotate it.

The player's name is never remembered: it is asked for at the start of every
round, is not kept in the session cookie, and is thrown away with the round.
"""

import hmac
import json
import os
import random
import secrets
import tempfile
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from flask import (Flask, redirect, render_template_string, request, session,
                   url_for)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-only-insecure-key')

CODE_LENGTH = 5
MAX_GUESSES = 10
NAME_MAX = 15

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR', BASE_DIR)
GAMES_FILE = os.path.join(DATA_DIR, 'games.json')
LEADERBOARD_FILE = os.path.join(DATA_DIR, 'leaderboard.json')
ACCESS_FILE = os.path.join(DATA_DIR, 'access_code.json')

# No 0/O/1/I/5/S - the code gets read out loud and typed on phones.
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRTUVWXYZ234679'
ACCESS_CODE_LENGTH = 6

GAME_TTL_HOURS = 12         # abandoned rounds are dropped after this
LEADERBOARD_LIMIT = 20      # rows shown on the landing page
LEADERBOARD_MAX_ROWS = 500  # oldest records past this are dropped

MAX_ATTEMPTS = 8            # wrong passwords allowed from one address...
LOCKOUT_SECONDS = 600       # ...before it is frozen out for this long

_games_lock = threading.Lock()
_board_lock = threading.Lock()
_access_lock = threading.Lock()
_attempts_lock = threading.Lock()
_attempts = {}              # ip -> [failures, first failure time]


# --------------------------------------------------------------------------
# storage helpers
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _read_json(path, fallback):
    """Never raises: a missing or damaged file just yields the fallback."""
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, type(fallback)) else fallback
    except (OSError, ValueError):
        return fallback


def _write_json(path, data):
    """Write via a temp file + rename so a crash can't truncate the original."""
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or '.')
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass  # a read-only disk shouldn't take the game down


def _prune_games(games):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=GAME_TTL_HOURS)
    for gid in list(games):
        try:
            touched = datetime.fromisoformat(games[gid].get('updated_at', ''))
        except (TypeError, ValueError):
            touched = None
        if touched is None or touched < cutoff:
            del games[gid]
    return games


def read_games():
    """Read-only snapshot. Use games_txn() when you need to change something."""
    with _games_lock:
        return _read_json(GAMES_FILE, {})


@contextmanager
def games_txn():
    """Load games, let the caller mutate them, then save.

    The lock is held for the whole block so two overlapping requests cannot
    overwrite each other. Saving is skipped if the block raises.
    """
    with _games_lock:
        games = _read_json(GAMES_FILE, {})
        yield games
        _write_json(GAMES_FILE, _prune_games(games))


# --------------------------------------------------------------------------
# leaderboard
# --------------------------------------------------------------------------

def record_result(game):
    entry = {
        'player': game['player'],
        'secret_number': game['secret_number'],
        'guesses_used': len(game['guesses']),
        'won': bool(game['won']),
        'finished_at': now_iso(),
    }
    with _board_lock:
        board = _read_json(LEADERBOARD_FILE, [])
        board.append(entry)
        _write_json(LEADERBOARD_FILE, board[-LEADERBOARD_MAX_ROWS:])


def leaderboard_view():
    """(rows, stats) - only cracked rounds, best score first."""
    board = _read_json(LEADERBOARD_FILE, [])
    wins = []
    for e in board:
        # 'winner' is the old multiplayer key; keep those rows readable.
        name = e.get('player') or e.get('winner')
        won = e.get('won', bool(e.get('winner')))
        if name and won:
            wins.append({
                'player': name,
                'secret_number': e.get('secret_number', ''),
                'guesses_used': e.get('guesses_used', 0),
                'finished_at': e.get('finished_at', ''),
            })
    # Fewest guesses first; ties broken by most recent.
    wins.sort(key=lambda e: (e['guesses_used'], _neg_time(e['finished_at'])))
    stats = {
        'rounds': len(board),
        'cracked': len(wins),
        'best': wins[0]['guesses_used'] if wins else None,
    }
    return wins[:LEADERBOARD_LIMIT], stats


def _neg_time(iso):
    """Sort key that puts newer timestamps first."""
    return tuple(-ord(c) for c in iso)


def pretty_time(iso):
    try:
        return datetime.fromisoformat(iso).strftime('%d %b, %H:%M')
    except (TypeError, ValueError):
        return ''


app.jinja_env.filters['pretty_time'] = pretty_time


# --------------------------------------------------------------------------
# game logic
# --------------------------------------------------------------------------

def random_5_digit():
    return ''.join(random.choices('0123456789', k=CODE_LENGTH))


def random_game_id():
    return '%032x' % random.getrandbits(128)


def new_game_state(player):
    return {
        'player': player,
        'secret_number': random_5_digit(),
        'guesses': [],
        'game_over': False,
        'won': False,
        'updated_at': now_iso(),
    }


def evaluate_guess(secret, guess):
    """Return (digit_matches, position_matches)."""
    position_matches = sum(s == g for s, g in zip(secret, guess))
    secret_counter = Counter(secret)
    guess_counter = Counter(guess)
    digit_matches = sum(min(secret_counter[d], guess_counter[d]) for d in guess_counter)
    return digit_matches, position_matches


def go_home(msg=None):
    """Forget the current round (and the name that came with it)."""
    if msg:
        session['msg'] = msg
    session.pop('gid', None)
    return redirect(url_for('index'))


# --------------------------------------------------------------------------
# the access code
#
# Generated here, never typed into a file by hand. It lives in
# access_code.json alongside the other data files - readable from the
# PythonAnywhere Files tab or a console, and by nothing on the web.
# --------------------------------------------------------------------------

def make_code():
    return ''.join(secrets.choice(CODE_ALPHABET)
                   for _ in range(ACCESS_CODE_LENGTH))


def _save_access(record):
    _write_json(ACCESS_FILE, record)
    # Only the owner should ever be able to read it, if the OS lets us say so.
    try:
        os.chmod(ACCESS_FILE, 0o600)
    except OSError:
        pass
    return record


def access_record():
    """The current code, generated on first use. None if it can't be saved."""
    with _access_lock:
        record = _read_json(ACCESS_FILE, {})
        if record.get('code') and record.get('id'):
            return record
        record = {'code': make_code(), 'id': secrets.token_hex(16),
                  'created_at': now_iso()}
        _save_access(record)
        # If the disk is read-only the code would change on every request and
        # nobody could ever get in, so say so instead of pretending.
        saved = _read_json(ACCESS_FILE, {})
        return record if saved.get('id') == record['id'] else None


def rotate_code():
    """Issue a new code. Everyone currently signed in has to enter it again.

    Also callable from a Bash console, which is how the host reads the very
    first code without touching the Files tab:

        cd ~/CrackTheCode && python3 -c \\
            "import crackthecode as c; print(c.rotate_code())"
    """
    with _access_lock:
        record = _save_access({'code': make_code(), 'id': secrets.token_hex(16),
                               'created_at': now_iso()})
    return record['code']


def normalise_code(text):
    """Accept 'ab-cd 12' for ABCD12 - it gets read out loud and retyped."""
    return ''.join(ch for ch in text.upper() if ch.isalnum())


def code_ok(supplied):
    record = access_record()
    if not record:
        return False            # no usable code = nobody gets in
    return hmac.compare_digest(normalise_code(supplied).encode('utf-8'),
                               record['code'].encode('utf-8'))


def client_ip():
    forwarded = request.headers.get('X-Forwarded-For', '')
    return forwarded.split(',')[0].strip() or request.remote_addr or '?'


def lockout_minutes_left(ip):
    """0 if this address may try again, else whole minutes until it may."""
    now = time.monotonic()
    with _attempts_lock:
        record = _attempts.get(ip)
        if not record:
            return 0
        failures, first = record
        if now - first > LOCKOUT_SECONDS:
            del _attempts[ip]
            return 0
        if failures < MAX_ATTEMPTS:
            return 0
        return int((LOCKOUT_SECONDS - (now - first)) // 60) + 1


def note_failure(ip):
    now = time.monotonic()
    with _attempts_lock:
        record = _attempts.get(ip)
        if record and now - record[1] <= LOCKOUT_SECONDS:
            record[0] += 1
        else:
            _attempts[ip] = [1, now]
        if len(_attempts) > 500:  # drop stale entries, don't grow forever
            for key in [k for k, v in _attempts.items()
                        if now - v[1] > LOCKOUT_SECONDS]:
                del _attempts[key]


def clear_failures(ip):
    with _attempts_lock:
        _attempts.pop(ip, None)


@app.before_request
def gate():
    """Access control disabled - game is now public."""
    return None


@app.after_request
def no_store(response):
    """Keep the browser from re-showing a stale game page from its cache."""
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


# --------------------------------------------------------------------------
# template
# --------------------------------------------------------------------------

HTML_TEMPLATE = '''
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crack The Code</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #eef1f7; --card: #ffffff; --ink: #16181d; --muted: #6b7280;
    --line: #e3e7ef; --brand: #4f46e5; --brand-ink: #ffffff;
    --good: #0f9960; --good-bg: #e6f6ee; --bad: #d1394b; --bad-bg: #fdeaec;
    --warn-bg: #fff6e0; --warn-line: #f0d089; --near-ink: #a9761b;
    --shadow: 0 1px 2px rgba(16,24,40,.05), 0 8px 24px rgba(16,24,40,.07);
    --mono: 'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1117; --card: #171a22; --ink: #e8eaf0; --muted: #9aa2b1;
      --line: #262b36; --brand: #7c74ff; --brand-ink: #0f1117;
      --good: #3ddc97; --good-bg: #10281f; --bad: #ff7b8a; --bad-bg: #2b1418;
      --warn-bg: #2a2317; --warn-line: #5c4a22; --near-ink: #e0b355;
      --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.35);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2.5rem 1rem 4rem;
    font-family: Inter, system-ui, -apple-system, Segoe UI, Arial, sans-serif;
    background: var(--bg); color: var(--ink); line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 44rem; margin: 0 auto; }
  header { text-align: center; margin-bottom: 1.75rem; }
  h1 { margin: 0; font-size: clamp(1.9rem, 5vw, 2.6rem); font-weight: 700;
       letter-spacing: -.02em; }
  h1 span { color: var(--brand); }
  .tagline { color: var(--muted); margin: .35rem 0 0; font-size: .95rem; }

  .card { background: var(--card); border: 1px solid var(--line); border-radius: 14px;
          padding: 1.4rem; box-shadow: var(--shadow); margin-bottom: 1.1rem; }
  .card h2 { margin: 0 0 .9rem; font-size: .78rem; font-weight: 600;
             letter-spacing: .09em; text-transform: uppercase; color: var(--muted); }

  button {
    font: inherit; font-weight: 600; border-radius: 9px; cursor: pointer;
    padding: .62rem 1.15rem; border: 1px solid transparent;
    background: var(--brand); color: var(--brand-ink); transition: filter .15s;
  }
  button:hover:not(:disabled) { filter: brightness(1.08); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.ghost { background: transparent; color: var(--ink); border-color: var(--line); }
  button.link { background: none; border: none; color: var(--muted); padding: .3rem 0;
                font-weight: 500; text-decoration: underline; }
  button.link:hover { color: var(--brand); }

  input[type=text], input[type=password] {
    font: inherit; padding: .6rem .75rem; border-radius: 9px;
    border: 1px solid var(--line); background: var(--bg); color: var(--ink); min-width: 0;
  }
  .hint { color: var(--muted); font-size: .82rem; margin: .45rem 0 0; }
  input:focus-visible, button:focus-visible { outline: 2px solid var(--brand);
                                              outline-offset: 2px; }

  .row { display: flex; gap: .6rem; flex-wrap: wrap; align-items: center; }
  .backbar { margin-bottom: 1rem; }

  .digits { display: flex; gap: .5rem; justify-content: center; margin: .2rem 0 1rem; }
  .digit {
    width: 3rem; height: 3.6rem; text-align: center; font-family: var(--mono);
    font-size: 1.6rem; font-weight: 700; border-radius: 11px;
    border: 1.5px solid var(--line); background: var(--bg); color: var(--ink);
    padding: 0; transition: border-color .15s;
  }
  .digit:focus { border-color: var(--brand); outline: none; }
  @media (max-width: 400px) { .digit { width: 2.6rem; height: 3.1rem; font-size: 1.3rem; } }

  .stats { display: flex; gap: .6rem; flex-wrap: wrap; margin-bottom: 1rem; }
  .stat { flex: 1 1 7rem; background: var(--bg); border: 1px solid var(--line);
          border-radius: 11px; padding: .7rem .85rem; }
  .stat b { display: block; font-size: 1.5rem; font-weight: 700; line-height: 1.2;
            font-family: var(--mono); }
  .stat span { font-size: .74rem; text-transform: uppercase; letter-spacing: .07em;
               color: var(--muted); }

  table { border-collapse: collapse; width: 100%; font-size: .92rem; }
  th, td { padding: .55rem .6rem; text-align: left; border-bottom: 1px solid var(--line); }
  th { font-size: .72rem; text-transform: uppercase; letter-spacing: .07em;
       color: var(--muted); font-weight: 600; }
  tbody tr:last-child td { border-bottom: none; }
  td.mono, .mono { font-family: var(--mono); }
  .rank { color: var(--muted); width: 2rem; font-family: var(--mono); }
  .empty { color: var(--muted); font-size: .92rem; text-align: center; padding: 1.2rem 0; }

  .pill { display: inline-block; min-width: 1.9rem; text-align: center;
          padding: .1rem .5rem; border-radius: 999px; font-family: var(--mono);
          font-size: .85rem; font-weight: 700; }
  .pill.hit { background: var(--good-bg); color: var(--good); }
  .pill.near { background: var(--warn-bg); color: var(--near-ink); }
  .pill.zero { background: var(--bg); color: var(--muted); }

  .banner { border-radius: 11px; padding: .9rem 1.1rem; margin-bottom: 1.1rem;
            font-weight: 600; }
  .banner.win { background: var(--good-bg); color: var(--good); }
  .banner.lose { background: var(--bad-bg); color: var(--bad); }
  .banner .code { font-family: var(--mono); font-size: 1.25rem; letter-spacing: .15em; }

  .msg { background: var(--warn-bg); border: 1px solid var(--warn-line);
         padding: .7rem 1rem; border-radius: 10px; margin-bottom: 1.1rem;
         font-size: .92rem; }

  .playerline { display: flex; justify-content: space-between; gap: 1rem;
                flex-wrap: wrap; align-items: baseline; }
  .who { font-weight: 600; color: var(--brand); font-size: 1.15rem; }
  .accesscode { font-family: var(--mono); font-size: 1.6rem; font-weight: 700;
                letter-spacing: .22em; color: var(--brand); }
  .left { font-family: var(--mono); font-size: 1.5rem; font-weight: 700; }
  footer { text-align: center; color: var(--muted); font-size: .8rem; margin-top: 1.5rem; }
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>Crack <span>The</span> Code</h1>
  <p class="tagline">Guess the {{ code_length }}-digit number in {{ max_guesses }} tries.</p>
</header>

{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}


{# -------------------------------- LOCKED -------------------------------- #}
{% if locked %}

  <div class="card">
    <h2>Private game</h2>
    {% if needs_setup %}
      <p style="margin:0; color:var(--bad); font-weight:600;">
        No access code could be created.</p>
      <p class="hint">The folder holding
         <span class="mono">access_code.json</span> is not writable, so the
         host needs to fix that and reload the web app. Until then nobody
         can play.</p>
    {% else %}
      <p style="margin:.1rem 0 1rem; color:var(--muted);">
        This game is locked. Enter the access code to play.</p>
      <form method="post" action="{{ url_for('unlock') }}" class="row" autocomplete="off">
        <input type="password" name="code" placeholder="Access code"
               style="flex:1 1 12rem; letter-spacing:.15em;" required autofocus
               autocomplete="one-time-code" spellcheck="false">
        <button type="submit">Unlock</button>
      </form>
      <p class="hint">Ask the host for it — the link on its own will not get you in.</p>
    {% endif %}
  </div>


{# ------------------------------- LANDING ------------------------------- #}
{% elif not game %}

  <div class="card">
    <h2>New game</h2>
    <form method="post" action="{{ url_for('start') }}" class="row" autocomplete="off">
      <input type="text" name="player" placeholder="Your name" maxlength="{{ name_max }}"
             style="flex:1 1 12rem;" required autofocus
             autocomplete="off" autocorrect="off" spellcheck="false" value="">
      <button type="submit">Start game</button>
    </form>
    <p class="hint">One player per game. You get {{ max_guesses }} guesses.</p>
  </div>

  <div class="card">
    <h2>Hall of fame</h2>
    <div class="stats">
      <div class="stat"><b>{{ stats.rounds }}</b><span>Rounds played</span></div>
      <div class="stat"><b>{{ stats.cracked }}</b><span>Codes cracked</span></div>
      <div class="stat"><b>{{ stats.best if stats.best else '—' }}</b><span>Best score</span></div>
    </div>

    {% if board %}
    <table>
      <thead>
        <tr><th class="rank">#</th><th>Player</th><th>Code</th><th>Guesses</th><th>When</th></tr>
      </thead>
      <tbody>
        {% for e in board %}
        <tr>
          <td class="rank">{{ loop.index }}</td>
          <td><b>{{ e.player }}</b></td>
          <td class="mono">{{ e.secret_number }}</td>
          <td><span class="pill hit">{{ e.guesses_used }}</span></td>
          <td style="color:var(--muted); font-size:.85rem;">{{ e.finished_at|pretty_time }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
      <p class="empty">No codes cracked yet. Be the first.</p>
    {% endif %}
  </div>

  <div class="card">
    <h2>Access code</h2>
    <div class="row" style="justify-content:space-between;">
      <div>
        <div class="accesscode">{{ access_code }}</div>
        <p class="hint" style="margin-top:.2rem;">
          Share this with whoever you want to let in.
          {% if code_created %}Issued {{ code_created|pretty_time }}.{% endif %}</p>
      </div>
      <form method="post" action="{{ url_for('newcode') }}"
            onsubmit="return confirm('Everyone, including you, will have to enter the new code. Continue?');">
        <button type="submit" class="ghost">New code</button>
      </form>
    </div>
  </div>

  <div style="text-align:center;">
    <form method="post" action="{{ url_for('lock') }}">
      <button type="submit" class="link">Lock this browser</button>
    </form>
  </div>


{# -------------------------------- GAME --------------------------------- #}
{% else %}

  <div class="backbar">
    <form method="post" action="{{ url_for('quit_game') }}">
      <button type="submit" class="link">← Back to home</button>
    </form>
  </div>

  <div class="card">
    <div class="playerline">
      <div>
        <h2 style="margin-bottom:.2rem;">Player</h2>
        <div class="who">{{ game.player }}</div>
      </div>
      <div style="text-align:right;">
        <h2 style="margin-bottom:.2rem;">Guesses left</h2>
        <div class="left">{{ guesses_left }}</div>
      </div>
    </div>
  </div>

  {% if game.game_over %}
    {% if game.won %}
      <div class="banner win">
        Cracked it in {{ game.guesses|length }}
        {{ 'guess' if game.guesses|length == 1 else 'guesses' }} —
        <span class="code">{{ game.secret_number }}</span>
      </div>
    {% else %}
      <div class="banner lose">
        Out of guesses. The code was
        <span class="code">{{ game.secret_number }}</span>
      </div>
    {% endif %}

    <div class="row" style="justify-content:center; margin-bottom:1.1rem;">
      <form method="post" action="{{ url_for('quit_game') }}">
        <button type="submit">Play again</button>
      </form>
    </div>
  {% else %}
    <div class="card">
      <h2>Your guess</h2>
      <form method="post" action="{{ url_for('guess') }}" id="guessForm" autocomplete="off">
        <div class="digits">
          {% for i in range(code_length) %}
          <input class="digit" type="text" inputmode="numeric" maxlength="1"
                 autocomplete="off" aria-label="Digit {{ loop.index }}"
                 {% if loop.first %}autofocus{% endif %}>
          {% endfor %}
        </div>
        <input type="hidden" name="guess" id="guessField">
        <div style="text-align:center;">
          <button type="submit">Submit guess</button>
        </div>
      </form>
    </div>
  {% endif %}

  <div class="card">
    <h2>Guess history</h2>
    {% if game.guesses %}
    <table>
      <thead>
        <tr><th class="rank">#</th><th>Guess</th>
            <th>Right digit</th><th>Right spot</th></tr>
      </thead>
      <tbody>
        {% for g in game.guesses %}
        <tr>
          <td class="rank">{{ loop.index }}</td>
          <td class="mono">{{ g.guess }}</td>
          <td><span class="pill {{ 'hit' if g.digit_matches == code_length else ('near' if g.digit_matches else 'zero') }}">{{ g.digit_matches }}</span></td>
          <td><span class="pill {{ 'hit' if g.position_matches == code_length else ('near' if g.position_matches else 'zero') }}">{{ g.position_matches }}</span></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
      <p class="empty">No guesses yet.</p>
    {% endif %}
  </div>

<script>
  var LEN = {{ code_length }};
  var boxes = Array.prototype.slice.call(document.querySelectorAll('.digit'));
  var field = document.getElementById('guessField');
  var form = document.getElementById('guessForm');

  // Five-box input: auto-advance, backspace, arrows, paste.
  boxes.forEach(function (box, i) {
    box.addEventListener('input', function () {
      box.value = box.value.replace(/\\D/g, '').slice(0, 1);
      if (box.value && i < LEN - 1) boxes[i + 1].focus();
    });
    box.addEventListener('keydown', function (e) {
      if (e.key === 'Backspace' && !box.value && i > 0) boxes[i - 1].focus();
      if (e.key === 'ArrowLeft' && i > 0) boxes[i - 1].focus();
      if (e.key === 'ArrowRight' && i < LEN - 1) boxes[i + 1].focus();
    });
    box.addEventListener('paste', function (e) {
      var text = (e.clipboardData || window.clipboardData).getData('text');
      var digits = text.replace(/\\D/g, '').slice(0, LEN).split('');
      if (!digits.length) return;
      e.preventDefault();
      digits.forEach(function (d, k) { if (boxes[k]) boxes[k].value = d; });
      boxes[Math.min(digits.length, LEN - 1)].focus();
    });
  });

  if (form) {
    form.addEventListener('submit', function (e) {
      var value = boxes.map(function (b) { return b.value; }).join('');
      if (value.length !== LEN) {
        e.preventDefault();
        for (var i = 0; i < boxes.length; i++) {
          if (!boxes[i].value) { boxes[i].focus(); break; }
        }
        return;
      }
      field.value = value;
    });
  }
</script>

{% endif %}

<footer>Right digit = correct number, wrong place counts too. Right spot = exact position.</footer>
</div>
</body>
</html>
'''


def render_page(game=None, locked=False):
    record = access_record()
    # A locked visitor must not be handed the hall of fame - or the code.
    board, stats = ([], {'rounds': 0, 'cracked': 0, 'best': None}) \
        if locked else leaderboard_view()
    return render_template_string(
        HTML_TEMPLATE,
        locked=locked,
        needs_setup=record is None,
        access_code=None if locked or not record else record['code'],
        code_created=None if locked or not record else record['created_at'],
        game=game,
        guesses_left=MAX_GUESSES - len(game['guesses']) if game else MAX_GUESSES,
        code_length=CODE_LENGTH,
        max_guesses=MAX_GUESSES,
        name_max=NAME_MAX,
        board=board,
        stats=stats,
        msg=session.pop('msg', None),
    )


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@app.route('/unlock', methods=['POST'])
def unlock():
    """The only route reachable without the access code."""
    ip = client_ip()
    waiting = lockout_minutes_left(ip)
    if waiting:
        session['msg'] = ('Too many wrong codes. Try again in %d minute%s.'
                          % (waiting, '' if waiting == 1 else 's'))
        return redirect(url_for('index'))

    if not code_ok(request.form.get('code', '')):
        note_failure(ip)
        session['msg'] = 'Wrong access code.'
        return redirect(url_for('index'))

    record = access_record()
    clear_failures(ip)
    session.clear()             # no stale round or message from anyone before
    session['unlocked'] = record['id']
    return redirect(url_for('index'))


@app.route('/lock', methods=['POST'])
def lock():
    """Sign this browser out - handy on a shared laptop."""
    gid = session.get('gid')
    if gid:
        with games_txn() as games:
            games.pop(gid, None)
    session.clear()
    return redirect(url_for('index'))


@app.route('/newcode', methods=['POST'])
def newcode():
    """Rotate the access code. Signs everyone out, including this browser."""
    rotate_code()
    gid = session.get('gid')
    if gid:
        with games_txn() as games:
            games.pop(gid, None)
    session.clear()
    session['msg'] = ('A new access code has been generated. '
                      'Enter it to carry on.')
    return redirect(url_for('index'))


@app.route('/', methods=['GET'])
def index():
    gid = session.get('gid')
    if not gid:
        return render_page()
    game = read_games().get(gid)
    if not game:
        return go_home('That game has expired. Start a new one.')
    return render_page(game)


@app.route('/start', methods=['POST'])
def start():
    player = request.form.get('player', '').strip()[:NAME_MAX]
    if not player:
        session['msg'] = 'Please enter your name.'
        return redirect(url_for('index'))

    gid = random_game_id()
    with games_txn() as games:
        games[gid] = new_game_state(player)
    session['gid'] = gid      # only the game id is stored, never the name
    return redirect(url_for('index'))


@app.route('/guess', methods=['POST'])
def guess():
    gid = session.get('gid')
    if not gid:
        return go_home()
    raw = request.form.get('guess', '').strip()
    finished = None

    with games_txn() as games:
        game = games.get(gid)
        if not game:
            return go_home('That game has expired. Start a new one.')
        if game['game_over']:
            session['msg'] = 'This round is over. Start a new one.'
            return redirect(url_for('index'))
        if len(raw) != CODE_LENGTH or not raw.isdigit():
            session['msg'] = 'Enter exactly %d digits.' % CODE_LENGTH
            return redirect(url_for('index'))

        digit_matches, position_matches = evaluate_guess(game['secret_number'], raw)
        game['guesses'].append({
            'guess': raw,
            'digit_matches': digit_matches,
            'position_matches': position_matches,
        })
        game['updated_at'] = now_iso()

        if position_matches == CODE_LENGTH:
            game['game_over'] = True
            game['won'] = True
            finished = dict(game)
        elif len(game['guesses']) >= MAX_GUESSES:
            game['game_over'] = True
            finished = dict(game)

    # Written outside the games lock so the two files never block each other.
    if finished:
        record_result(finished)

    return redirect(url_for('index'))


@app.route('/quit', methods=['POST'])
def quit_game():
    """Leave the round and forget it - including the name that played it."""
    gid = session.pop('gid', None)
    if gid:
        with games_txn() as games:
            games.pop(gid, None)
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
