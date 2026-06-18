#!/usr/bin/env python3
import os, json, time, sys, requests
from datetime import datetime, timezone, timedelta

PLAYER_ID      = 702275   # JR Ritchie
TEAM_ID        = '144'    # Atlanta Braves
STATE_FILE     = 'poller/state.json'
MLB            = 'https://statsapi.mlb.com/api/v1'
POLL_INTERVAL  = 30       # seconds between checks
LOOP_DURATION  = 270      # run for 4.5 min so 5-min cron jobs don't overlap
REPORT_EVERY   = 900      # 15-minute status reports (seconds)

def log(msg):
    print(msg, flush=True)

def mlb(path, **params):
    try:
        r = requests.get(f'{MLB}/{path}', params=params, timeout=10)
        return r.json() if r.ok else None
    except Exception as e:
        log(f'MLB API error: {e}')
        return None

def today():
    # Use Eastern time (UTC-4 EDT) so date matches MLB schedule
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    return et.strftime('%Y-%m-%d')

def now_ts():
    return int(time.time())

def game_state(g):
    c   = g.get('status', {}).get('codedGameState', '')
    det = g.get('status', {}).get('detailedState', '').lower()
    if c in ('I', 'IR', 'MC') and 'delay' not in det:
        return 'live'
    if c in ('F', 'O'):
        return 'final'
    return 'pre'

def inn_txt(ls):
    half = '▲' if ls.get('inningHalf') == 'Top' else '▼'
    return f"{half}{ls.get('currentInning', '?')}"

def calc_outing(plays):
    k = bb = h = r = outs = 0
    for p in plays:
        e = (p.get('result', {}).get('eventType') or '').lower()
        if 'strikeout' in e: k += 1
        elif e in ('walk', 'intent_walk'): bb += 1
        elif e in ('single', 'double', 'triple', 'home_run'): h += 1
        if p.get('result', {}).get('isOut'):
            outs += 2 if 'double_play' in e else 3 if 'triple_play' in e else 1
        r += int(p.get('result', {}).get('rbi') or 0)
    f, pt = divmod(outs, 3)
    return dict(ip=f'{f}.{pt}' if pt else str(f), k=k, bb=bb, h=h, r=r)

def ol(o):
    return f"`{o['ip']} IP · {o['k']}K · {o['bb']}BB · {o['h']}H · {o['r']}R`"

def post_slack(wh, payload):
    if not wh:
        log('No Slack webhook set.')
        return
    try:
        resp = requests.post(wh, json=payload, timeout=10)
        log(f'Slack response: {resp.status_code}')
    except Exception as e:
        log(f'Slack error: {e}')

def msg_entry(game, inn):
    a, h = game['teams']['away'], game['teams']['home']
    return {'text': '⚾ J.R. Ritchie is now pitching!', 'blocks': [
        {'type': 'header', 'text': {'type': 'plain_text', 'text': '⚾  J.R. Ritchie Is Now Pitching!', 'emoji': True}},
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': f"*{a['team']['name']}* vs *{h['team']['name']}*"}},
        {'type': 'section', 'fields': [
            {'type': 'mrkdwn', 'text': f"*Inning*\n{inn}"},
            {'type': 'mrkdwn', 'text': f"*Score*\n{a['score']}–{h['score']}"}
        ]},
        {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': "You'll get a status update every 15 min while he's on the mound."}]}
    ]}

def msg_report(outing, game, inn):
    a, h = game['teams']['away'], game['teams']['home']
    perf = '🟢' if outing['r'] == 0 else '🟡' if outing['r'] <= 1 else '🔴'
    return {'text': f"📊 Ritchie update — {outing['ip']} IP · {outing['k']}K · {outing['r']}R", 'blocks': [
        {'type': 'header', 'text': {'type': 'plain_text', 'text': '📊  Ritchie — 15-Min Update', 'emoji': True}},
        {'type': 'section', 'fields': [
            {'type': 'mrkdwn', 'text': f"*Outing Line*\n{ol(outing)}"},
            {'type': 'mrkdwn', 'text': f"*Inning*\n{inn}"}
        ]},
        {'type': 'section', 'fields': [
            {'type': 'mrkdwn', 'text': f"*Score*\n{a['team']['name']} {a['score']} – {h['score']} {h['team']['name']}"},
            {'type': 'mrkdwn', 'text': f"*Status*\n{perf} Still on the mound"}
        ]}
    ]}

def msg_done(outing, game):
    a, h = game['teams']['away'], game['teams']['home']
    perf = '🟢 Clean' if outing['r'] == 0 else '🟡 Solid' if outing['r'] <= 1 else '🔴 Rough'
    return {'text': f"✅ Ritchie done — {outing['ip']} IP · {outing['k']}K · {outing['r']}R", 'blocks': [
        {'type': 'header', 'text': {'type': 'plain_text', 'text': '✅  Outing Complete — J.R. Ritchie', 'emoji': True}},
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': f"{perf} outing\n{ol(outing)}"}},
        {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': f"{a['team']['name']} @ {h['team']['name']} · {a['score']}–{h['score']}"}]}
    ]}

def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        if s.get('date') != today():
            log(f"New day ({today()}), resetting state.")
            raise ValueError('new day')
        return s
    except Exception:
        return {'date': today(), 'is_pitching': False, 'last_ab_idx': -1,
                'entry_sent': False, 'last_report_ts': 0}

def save_state(s):
    with open(STATE_FILE, 'w') as f:
        json.dump(s, f)

def poll_once(state, wh):
    d     = mlb('schedule', sportId=1, date=today(), hydrate='linescore,team')
    dates = (d or {}).get('dates', [])
    if not dates:
        log(f'No games found for {today()} (empty dates)')
        return
    games = dates[0].get('games', [])
    log(f'Found {len(games)} game(s) on {today()}')

    game = next((g for g in games
                 if TEAM_ID in (str(g['teams']['home']['team']['id']),
                                str(g['teams']['away']['team']['id']))), None)

    if not game:
        log(f'No Braves game found.')
        return

    gs     = game_state(game)
    coded  = game.get('status', {}).get('codedGameState', '?')
    detail = game.get('status', {}).get('detailedState', '?')
    log(f'Braves game found — state={gs} coded={coded} detail={detail}')

    if gs not in ('live', 'final'):
        return

    try:
        feed = requests.get(
            f'https://statsapi.mlb.com/api/v1.1/game/{game["gamePk"]}/feed/live',
            timeout=15).json()
    except Exception as e:
        log(f'Feed error: {e}')
        return

    ls        = feed.get('liveData', {}).get('linescore', {})
    all_plays = feed.get('liveData', {}).get('plays', {}).get('allPlays', [])
    cur_play  = feed.get('liveData', {}).get('plays', {}).get('currentPlay')

    cur_pitcher_id = str(ls.get('defense', {}).get('pitcher', {}).get('id', ''))
    cur_pitcher_nm = ls.get('defense', {}).get('pitcher', {}).get('fullName', '?')
    log(f'Current pitcher: {cur_pitcher_nm} (id={cur_pitcher_id}), looking for id={PLAYER_ID}')

    my_id    = str(PLAYER_ID)
    is_cur   = cur_pitcher_id == my_id
    my_plays = [p for p in all_plays if str(p.get('matchup', {}).get('pitcher', {}).get('id', '')) == my_id]
    my_done  = [p for p in my_plays if p.get('result', {}).get('eventType')]
    outing   = calc_outing(my_done)
    inn      = inn_txt(ls)
    cur_idx  = (cur_play or {}).get('atBatIndex', -1)

    log(f'is_cur={is_cur} inn={inn} my_plays={len(my_plays)} entry_sent={state["entry_sent"]}')

    # Entry notification
    if is_cur and not state['entry_sent']:
        post_slack(wh, msg_entry(game, inn))
        state['entry_sent']     = True
        state['is_pitching']    = True
        state['last_report_ts'] = now_ts()
        log(f'Entry sent — {inn}')

    # 15-minute status report while he's on the mound
    if is_cur and state['entry_sent']:
        elapsed_since_report = now_ts() - state.get('last_report_ts', 0)
        if elapsed_since_report >= REPORT_EVERY:
            post_slack(wh, msg_report(outing, game, inn))
            state['last_report_ts'] = now_ts()
            log(f'15-min report sent — {inn}')

    if is_cur:
        state['last_ab_idx']  = cur_idx
        state['is_pitching']  = True

    # Outing complete
    if not is_cur and state['is_pitching'] and my_done:
        post_slack(wh, msg_done(outing, game))
        state['is_pitching'] = False
        log(f"Done sent — {outing['ip']} IP · {outing['k']}K · {outing['r']}R")

    state['date'] = today()

def main():
    wh = os.environ.get('SLACK_WEBHOOK_URL', '')
    log(f'Poller starting — ET date: {today()}, player_id: {PLAYER_ID}, team_id: {TEAM_ID}')

    if os.environ.get('TEST_MODE', '').lower() == 'true':
        post_slack(wh, {
            'text': '✅ Ritchie Tracker is connected!',
            'blocks': [
                {'type': 'header', 'text': {'type': 'plain_text', 'text': '✅  Ritchie Tracker — Test Successful', 'emoji': True}},
                {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'GitHub Actions poller is running and your Slack webhook is connected.\n\nYou\'ll get notified when Ritchie enters a game, with a status update every 15 minutes while he\'s on the mound.'}},
                {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': 'Polling every 30 sec · ldkobs/ritchie-tracker'}]}
            ]
        })
        log('Test message sent.')
        return

    if os.environ.get('TEST_GAME', '').lower() == 'true':
        fake_game = {'teams': {'away': {'team': {'name': 'Milwaukee Brewers'}, 'score': 2},
                               'home': {'team': {'name': 'Atlanta Braves'},    'score': 3}}}
        fake_outing_mid  = dict(ip='0.2', k=1, bb=0, h=1, r=0)
        final_outing     = dict(ip='1.0', k=2, bb=1, h=1, r=0)

        log('Sending entry…')
        post_slack(wh, msg_entry(fake_game, '▼7'))
        time.sleep(8)

        log('Sending 15-min report…')
        post_slack(wh, msg_report(fake_outing_mid, fake_game, '▼7'))
        time.sleep(8)

        log('Sending outing complete…')
        post_slack(wh, msg_done(final_outing, fake_game))
        log('Simulation done.')
        return

    state = load_state()
    # Ensure last_report_ts exists in older state files
    state.setdefault('last_report_ts', 0)
    log(f'Loaded state: {state}')
    start     = time.time()
    iteration = 0

    while True:
        iteration += 1
        log(f'--- Poll #{iteration} ---')
        poll_once(state, wh)
        elapsed = time.time() - start
        if elapsed + POLL_INTERVAL >= LOOP_DURATION:
            break
        log(f'Sleeping 30s ({int(LOOP_DURATION - elapsed)}s remaining)…')
        sys.stdout.flush()
        time.sleep(POLL_INTERVAL)

    save_state(state)
    log(f'Done. Final state: {state}')

if __name__ == '__main__':
    main()
