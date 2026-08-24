import random
import string
import threading
import time
from flask import Flask, render_template_string, request, session, redirect, url_for
from flask_socketio import SocketIO, join_room, emit, leave_room
from collections import defaultdict

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'
socketio = SocketIO(app, async_mode='threading', manage_session=False)

TIMEOUT_SEC = 30

games = defaultdict(lambda: {
    'secret_number': '',
    'guesses': [],
    'players': [],
    'player_set': set(),
    'turn_index': 0,
    'game_over': False,
    'chat': [],
    'timer_thread': None,
    'remaining_seconds': TIMEOUT_SEC,
    'last_turn_timestamp': time.time(),
    'consec_skips': defaultdict(int)
})

HTML_TEMPLATE = '''
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Multiplayer Number Lock Game</title>
    <script src="//cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.0/socket.io.min.js"></script>
    <style>
      body { font-family: Arial, sans-serif; margin: 2em; }
      .container {display: flex;}
      .left-panel {flex:2;}
      .right-panel {flex:1; margin-left:2em; border-left:1px solid #aaa; padding-left:2em;}
      .win { color: green; font-size: 1.2em; }
      table { border-collapse: collapse; }
      th, td { padding: 4px 8px; border: 1px solid #888; }
      .player { font-weight: bold; }
      #chatbox { width: 100%; height: 250px; overflow-y:auto; background: #f3f3f3; border:1px solid #ccc; }
      #exitButton { margin-top: 1em; }
    </style>
</head>
<body>
{% if not group_code %}
    <form method="post" action="{{ url_for('create') }}">
        <button type="submit">Create Game</button>
    </form>
    <form method="post" action="{{ url_for('join') }}">
        <input type="text" name="group_code" placeholder="Enter Group Code" maxlength="6" required>
        <button type="submit">Join Game</button>
    </form>
{% elif not nickname %}
    <form method="post" action="{{ url_for('set_nickname', group_code=group_code) }}">
        <label>Enter nickname:</label>
        <input type="text" name="nickname" maxlength="15" required>
        <button type="submit">Join</button>
    </form>
{% else %}
<div class="container">
<div class="left-panel">
    <p>Your group code: <b>{{ group_code }}</b></p>
    <p>Your nickname: <span class="player">{{ nickname }}</span></p>
    <div id="turnIndicator">
        Player turn: <span id="currentTurn"></span>
    </div>
    <div>
        <b>Time left for turn:</b> <span id="timerDisplay">60</span> sec
    </div>
    <form id="guessForm">
        <label>Enter your 5-digit guess:</label>
        <input type="text" id="guessInput" name="guess" maxlength="5" pattern="[0-9]{5}" required>
        <button id="guessButton" type="submit">Guess</button>
    </form>
    <button id="exitButton">Exit Group</button>
    <h4>Guess History:</h4>
    <table id="history">
        <tr>
            <th>Player</th><th>Guess</th><th>Digit Matches</th><th>Position Matches</th>
        </tr>
        {% for g in guesses %}
        <tr>
            <td>{{ g['player'] }}</td>
            <td>{{ g['guess'] }}</td>
            <td>{{ g['digit_matches'] }}</td>
            <td>{{ g['position_matches'] }}</td>
        </tr>
        {% endfor %}
    </table>
    <div id="winMsg">{% if win %}<p class="win">
        Congratulations! <b>{{ winner }}</b> broke the lock.<br>
        Secret Number: <b>{{ secret_number }}</b>
        <form method="post" action="{{ url_for('reset_game', group_code=group_code) }}">
          <button type="submit">Play Again</button>
        </form>
    </p>{% endif %}</div>
</div>
<div class="right-panel">
  <b>Chat</b>
  <div id="chatbox"></div>
  <form id="chatForm" style="margin-top:0.5em;">
    <input type="text" id="chatInput" autocomplete="off" maxlength="80" placeholder="Type message" required style="width:80%;">
    <button type="submit">Send</button>
  </form>
</div>
</div>
<script>
    const playerName = "{{ nickname }}";
    const groupCode = "{{ group_code }}";
    let myTurn = false;
    let winState = {{ 'true' if win else 'false' }};
    const socket = io();
    socket.emit('join', {'group_code': groupCode, 'nickname': playerName });

    // GUESS SUBMIT
    document.getElementById('guessForm').onsubmit = function(e) {
        e.preventDefault();
        if (!myTurn || document.getElementById('guessInput').disabled) return;
        const guess = this.guess.value;
        socket.emit('submit_guess', {
            group_code: groupCode,
            nickname: playerName,
            guess: guess
        });
        this.guess.value = '';
    };

    // EXIT BUTTON logic: Wait for exit confirmation from server!
    document.getElementById('exitButton').onclick = function(){
        if(confirm('Are you sure you want to leave the group?')) {
            socket.emit('player_exit', {group_code: groupCode, nickname: playerName});
        }
    };
    // Listen for confirmation before redirecting!
    socket.on('exit_confirm', function(data){
        window.location.href = '/';
    });

    // CHAT SUBMIT
    document.getElementById('chatForm').onsubmit = function(e) {
        e.preventDefault();
        const msg = document.getElementById('chatInput').value;
        socket.emit('chat_message', {
            group_code: groupCode,
            nickname: playerName,
            message: msg
        });
        document.getElementById('chatInput').value = '';
    };

    function setTurn(turnPlayer) {
        document.getElementById('currentTurn').innerText = turnPlayer;
        myTurn = (turnPlayer === playerName);
        document.getElementById('guessInput').disabled = !myTurn || winState;
        document.getElementById('guessButton').disabled = !myTurn || winState;
    }

    socket.on('game_state', function(data) {
        let table = '<tr><th>Player</th><th>Guess</th><th>Digit Matches</th><th>Position Matches</th></tr>';
        data.guesses.forEach(function(g) {
            table += '<tr><td>' + g.player + '</td><td>' + g.guess + '</td><td>' + g.digit_matches + '</td><td>' + g.position_matches + '</td></tr>';
        });
        document.getElementById('history').innerHTML = table;
        winState = data.win;
        setTurn(data.current_turn_name);

        if(data.win){
            document.getElementById('winMsg').innerHTML =
                '<p class="win">Congratulations! <b>' + data.winner +
                '</b> broke the lock.<br>Secret Number: <b>' + data.secret_number +
                '</b></p><form method="post" action="/' + data.group_code + '/reset"><button type="submit">Play Again</button></form>';
            document.getElementById('guessInput').disabled = true;
            document.getElementById('guessButton').disabled = true;
            document.getElementById('exitButton').disabled = true;
        } else if (data.announce_exit) {
            document.getElementById('winMsg').innerHTML = data.announce_exit;
            document.getElementById('guessInput').disabled = true;
            document.getElementById('guessButton').disabled = true;
            document.getElementById('exitButton').disabled = true;
        } else {
            document.getElementById('winMsg').innerHTML = '';
            document.getElementById('exitButton').disabled = false;
        }
        document.getElementById('timerDisplay').innerText = data.remaining_seconds;
    });

    socket.on('timer_update', function(data){
        document.getElementById('timerDisplay').innerText = data.remaining_seconds;
    });

    socket.on('chat_history', function(data) {
        let c = '';
        data.chat.forEach(function(msg){
            c += '<div><b>' + msg.nickname + ':</b> ' + msg.message + '</div>';
        });
        document.getElementById('chatbox').innerHTML = c;
        document.getElementById('chatbox').scrollTop = document.getElementById('chatbox').scrollHeight;
    });
    socket.on('chat_message', function(data){
        let c = document.getElementById('chatbox').innerHTML;
        c += '<div><b>' + data.nickname + ':</b> ' + data.message + '</div>';
        document.getElementById('chatbox').innerHTML = c;
        document.getElementById('chatbox').scrollTop = document.getElementById('chatbox').scrollHeight;
    });

    socket.emit('get_state', {group_code: groupCode});
    socket.emit('get_chat', {group_code: groupCode});

    setInterval(function(){
        socket.emit('get_timer', {group_code: groupCode});
    }, 1000);
</script>
{% endif %}
</body></html>
'''

def random_5_digit():
    return ''.join(random.choices('0123456789', k=5))

def random_group_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def evaluate_guess(secret, guess):
    position_matches = sum(s == g for s, g in zip(secret, guess))
    from collections import Counter
    secret_counter = Counter(secret)
    guess_counter = Counter(guess)
    digit_matches = sum(min(secret_counter[d], guess_counter[d]) for d in guess_counter)
    return digit_matches, position_matches

def get_current_turn_name(game):
    if not game['players']:
        return ''
    return game['players'][game['turn_index'] % len(game['players'])]

def remove_player(game, nickname):
    if nickname in game['players']:
        idx = game['players'].index(nickname)
        game['players'].remove(nickname)
        game['player_set'].discard(nickname)
        if nickname in game['consec_skips']:
            del game['consec_skips'][nickname]
        if idx < game['turn_index']:
            game['turn_index'] -= 1
        elif idx == game['turn_index']:
            if game['players']:
                game['turn_index'] = game['turn_index'] % len(game['players'])
            else:
                game['turn_index'] = 0

def game_state_payload(game, group_code, announce_exit=None):
    win = (len(game['guesses']) > 0 and game['guesses'][-1]['position_matches'] == 5)
    return dict(
        guesses=game['guesses'],
        win=win,
        winner=game['guesses'][-1]['player'] if win else None,
        secret_number=game['secret_number'] if win else None,
        current_turn_name=get_current_turn_name(game),
        group_code=group_code,
        remaining_seconds=game['remaining_seconds'],
        announce_exit=announce_exit
    )

def stop_timer_thread(game):
    t = game.get('timer_thread')
    game['timer_thread'] = None
    if t and hasattr(t, "stop_event"):
        t.stop_event.set()

def start_turn_timer(group_code):
    game = games[group_code]
    stop_timer_thread(game)
    game['remaining_seconds'] = TIMEOUT_SEC
    game['last_turn_timestamp'] = time.time()
    def timer_thread_func(game_ref, group_code):
        thread = threading.current_thread()
        thread.stop_event = threading.Event()
        while True:
            elapsed = int(time.time() - game_ref['last_turn_timestamp'])
            left = TIMEOUT_SEC - elapsed
            if left < 0: left = 0
            game_ref['remaining_seconds'] = left
            socketio.emit('timer_update', {'remaining_seconds': left}, to=group_code)
            if left == 0 and not game_ref['game_over']:
                try:
                    current_player = get_current_turn_name(game_ref)
                    if not current_player:
                        break
                    game_ref['consec_skips'][current_player] += 1
                    skip_count = game_ref['consec_skips'][current_player]
                    if skip_count >= 4:
                        msg = f"{current_player} has been removed after 4 consecutive skips."
                        game_ref['chat'].append({'nickname': 'Game', 'message': msg})
                        remove_player(game_ref, current_player)
                        socketio.emit('chat_message', {'nickname': 'Game', 'message': msg}, to=group_code)
                        if not game_ref['players']:
                            game_ref['game_over'] = True
                            stop_timer_thread(game_ref)
                            announce_msg = "<p class='win'>All players left or were removed.</p>"
                            socketio.emit('game_state', game_state_payload(game_ref, group_code, announce_msg), to=group_code)
                            return
                        else:
                            game_ref['last_turn_timestamp'] = time.time()
                            game_ref['remaining_seconds'] = TIMEOUT_SEC
                            socketio.emit('game_state', game_state_payload(game_ref, group_code, f"<p class='win'>{msg}</p>"), to=group_code)
                            continue
                    skip_msg = f"{current_player} timed out. Turn skipped."
                    game_ref['chat'].append({'nickname': 'Game', 'message': skip_msg})
                    socketio.emit('chat_message', {'nickname': 'Game', 'message': skip_msg}, to=group_code)
                    game_ref['turn_index'] = (game_ref['turn_index'] + 1) % len(game_ref['players'])
                    game_ref['last_turn_timestamp'] = time.time()
                    game_ref['remaining_seconds'] = TIMEOUT_SEC
                    socketio.emit('game_state', game_state_payload(game_ref, group_code), to=group_code)
                except Exception:
                    game_ref['game_over'] = True
                    stop_timer_thread(game_ref)
                    msg = "<p class='win'>Game ended due to error.</p>"
                    socketio.emit('game_state', game_state_payload(game_ref, group_code, msg), to=group_code)
                    break
            if thread.stop_event.wait(timeout=1):
                break
            if game_ref['game_over']:
                break
    t = threading.Thread(target=timer_thread_func, args=(game, group_code), daemon=True)
    game['timer_thread'] = t
    t.start()

@app.route('/', methods=['GET'])
def index():
    group_code = session.get('group_code')
    nickname = session.get('nickname')
    guesses = []
    winner = None
    win = False
    secret_number = None
    if group_code and group_code in games:
        game = games[group_code]
        guesses = game['guesses']
        secret_number = game['secret_number']
        if guesses and guesses[-1]['position_matches'] == 5:
            win = True
            winner = guesses[-1]['player']
    return render_template_string(HTML_TEMPLATE,
        group_code=group_code, nickname=nickname,
        guesses=guesses, win=win, winner=winner,
        secret_number=secret_number)

@app.route('/create', methods=['POST'])
def create():
    code = random_group_code()
    game = games[code]
    stop_timer_thread(game)
    game['secret_number'] = random_5_digit()
    game['guesses'] = []
    game['players'] = []
    game['player_set'] = set()
    game['turn_index'] = 0
    game['game_over'] = False
    game['chat'] = []
    game['remaining_seconds'] = TIMEOUT_SEC
    game['last_turn_timestamp'] = time.time()
    game['consec_skips'] = defaultdict(int)
    session['group_code'] = code
    session.pop('nickname', None)
    return redirect(url_for('index'))

@app.route('/join', methods=['POST'])
def join():
    code = request.form['group_code'].upper()
    if code in games:
        session['group_code'] = code
        session.pop('nickname', None)
        return redirect(url_for('index'))
    return "<p>Group code not found. <a href='/'>Try again</a></p>"

@app.route('/<group_code>/set_nickname', methods=['POST'])
def set_nickname(group_code):
    nickname = request.form['nickname']
    game = games[group_code]
    if nickname not in game['player_set']:
        game['players'].append(nickname)
        game['player_set'].add(nickname)
        game['consec_skips'][nickname] = 0
    session['nickname'] = nickname
    if len(game['players']) == 1:
        start_turn_timer(group_code)
    return redirect(url_for('index'))

@app.route('/<group_code>/reset', methods=['POST'])
def reset_game(group_code):
    game = games[group_code]
    stop_timer_thread(game)
    game['secret_number'] = random_5_digit()
    game['guesses'] = []
    game['turn_index'] = 0
    game['game_over'] = False
    game['remaining_seconds'] = TIMEOUT_SEC
    game['last_turn_timestamp'] = time.time()
    game['consec_skips'] = defaultdict(int, {nick:0 for nick in game['players']})
    start_turn_timer(group_code)
    return redirect(url_for('index'))

@socketio.on('join')
def handle_join(data):
    join_room(data['group_code'])
    group_code = data['group_code']
    game = games[group_code]
    emit('game_state', game_state_payload(game, group_code), to=group_code)
    emit('chat_history', {'chat': game['chat']}, to=request.sid)
    if len(game['players']) > 0 and not game['game_over'] and not game['timer_thread']:
        start_turn_timer(group_code)

@socketio.on('get_state')
def on_get_state(data):
    group_code = data['group_code']
    game = games[group_code]
    emit('game_state', game_state_payload(game, group_code), to=request.sid)

@socketio.on('get_chat')
def on_get_chat(data):
    group_code = data['group_code']
    game = games[group_code]
    emit('chat_history', {'chat': game['chat']}, to=request.sid)

@socketio.on('get_timer')
def on_get_timer(data):
    group_code = data['group_code']
    game = games[group_code]
    emit('timer_update', {'remaining_seconds': game['remaining_seconds']}, to=request.sid)

@socketio.on('submit_guess')
def handle_submit(data):
    group_code = data['group_code']
    nickname = data['nickname']
    guess = data['guess']
    game = games[group_code]
    win = (len(game['guesses']) > 0 and game['guesses'][-1]['position_matches'] == 5)
    if win or game['game_over']:
        return
    if nickname != get_current_turn_name(game):
        return
    if not (len(guess) == 5 and guess.isdigit()):
        return
    secret_number = game['secret_number']
    digit_matches, position_matches = evaluate_guess(secret_number, guess)
    result = {
        'player': nickname,
        'guess': guess,
        'digit_matches': digit_matches,
        'position_matches': position_matches
    }
    game['guesses'].append(result)
    # Reset skip count on successful guess
    game['consec_skips'][nickname] = 0

    if position_matches == 5:
        game['game_over'] = True
        stop_timer_thread(game)
    else:
        # Move turn to next
        game['turn_index'] = (game['turn_index'] + 1) % len(game['players'])
        game['last_turn_timestamp'] = time.time()
        game['remaining_seconds'] = TIMEOUT_SEC
        start_turn_timer(group_code)

    socketio.emit('game_state', game_state_payload(game, group_code), to=group_code)

@socketio.on('player_exit')
def handle_player_exit(data):
    group_code = data['group_code']
    nickname = data['nickname']
    game = games[group_code]
    if nickname not in game['player_set']:
        # Defensive: still redirect exiting user
        emit('exit_confirm', {}, to=request.sid)
        return
    remove_player(game, nickname)
    leave_room(group_code)
    msg = f"{nickname} left the group."
    game['chat'].append({'nickname': 'Game', 'message': msg})
    socketio.emit('chat_message', {'nickname': 'Game', 'message': msg}, to=group_code)
    emit('exit_confirm', {}, to=request.sid)  # <--- Only to this user!
    if not game['players']:
        game['game_over'] = True
        stop_timer_thread(game)
        announce_msg = "<p class='win'>All players have left the group.</p>"
        socketio.emit('game_state', game_state_payload(game, group_code, announce_msg), to=group_code)
        return
    # If player leaving had the turn, advance to next
    game['turn_index'] = (game['turn_index']) % len(game['players'])
    game['last_turn_timestamp'] = time.time()
    game['remaining_seconds'] = TIMEOUT_SEC
    # Broadcast updated game state to remaining players!
    socketio.emit('game_state', game_state_payload(game, group_code, f"<p class='win'>{msg}</p>"), to=group_code)


@socketio.on('chat_message')
def handle_chat(data):
    group_code = data['group_code']
    nickname = data['nickname']
    message = data['message'][:80]
    game = games[group_code]
    chat_entry = {'nickname': nickname, 'message': message}
    game['chat'].append(chat_entry)
    socketio.emit('chat_message', chat_entry, to=group_code)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)