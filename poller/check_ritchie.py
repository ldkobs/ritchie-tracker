#!/usr/bin/env python3
import os, json, time, requests
from datetime import datetime, timezone

PLAYER_ID     = 702275   # JR Ritchie
TEAM_ID       = '144'    # Atlanta Braves
STATE_FILE    = 'poller/state.json'
MLB           = 'https://statsapi.mlb.com/api/v1'
POLL_INTERVAL = 30       # seconds between checks
LOOP_DURATION = 270      # run for 4.5 min so 5-min cron jobs don't overlap

def mlb(path, **params):
    try:
        r = requests.get(f'{MLB}/{path}', params=params, timeout=10)
        return r.json() if r.ok else None
    except Exception:
        return None

def today():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')

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
        return
    try:
        requests.post(wh, json=payload, timeout=10)
    except Exception:
        pass

def msg_entry(game, inn):
    a, h = game['teams']['away'], game['teams']['home']
    return {'text': '⚾ J.R. Ritchie is now pitching!', 'blocks': [
        {'type': 'header', 'text': {'type': 'plain_text', 'text': '⚾  J.R. Ritchie Is Now Pitching!', 'emoji': True}},
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': f"*{a['team']['name']}* vs *{h['team']['name']}*"}},
        {'type': 'section', 'fields': [
            {'type': 'mrkdwn', 'text': f"*Inning*\n{inn}"},
            {'type': 'mrkdwn', 'text': f"*Score*\n{a['score']}–{h['score']}"}
        ]},
        {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': "You'll get a message after each at-bat.  🔴 K · 🔵 BB · 🟡 Hit · ⚫ Out"}]}
    ]}

def msg_ab(play, outing, inn):
    evt  = (play.get('result', {}).get('eventType') or '').lower()
    name = play.get('result', {}).get('event') or 'Out'
    em   = '🔴' if 'strikeout' in evt else '🔵' if evt in ('walk', 'intent_walk') else '💣' if evt == 'home_run' else '🟡' if evt in ('single', 'double', 'triple') else '⚫'
    bat  = (play.get('matchup', {}).get('batter', {}).get('fullName') or '?')
    return {'text': f'{em} {name} — {bat} | {outing["ip"]} IP · {outing["k"]}K · {outing["r"]}R', 'blocks': [
        {'type': 'header', 'text': {'type': 'plain_text', 'text': f'{em}  {name}', 'emoji': True}},
        {'type': 'section', 'fields': [
            {'type': 'mrkdwn', 'text': f"*Batter*\n{bat}"},
            {'type': 'mrkdwn', 'text': f"*Inning*\n{inn}"}
        ]},
        {'type': 'divider'},
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': f"*Outing Line*\n{ol(outing)}"}}
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
            raise ValueError('new day')
        return s
    except Exception:
        return {'date': today(), 'is_pitching': False, 'last_ab_idx': -1, 'entry_sent': False}

def save_state(s):
    with open(STATE_FILE, 'w') as f:
        json.dump(s, f)

def poll_once(state, wh):
    d     = mlb('schedule', sportId=1, date=today(), hydrate='linescore,team')
    games = ((d or {}).get('dates') or [{}])[0].get('games', [])
    game  = next((g for g in games
                  if TEAM_ID in (str(g['teams']['home']['team']['id']),
                                  str(g['teams']['away']['team']['id']))), None)

    if not game or game_state(game) not in ('live', 'final'):
        return

    try:
        feed = requests.get(
            f'https://statsapi.mlb.com/api/v1.1/game/{game["gamePk"]}/feed/live',
            timeout=15).json()
    except Exception:
        return

    ls        = feed.get('liveData', {}).get('linescore', {})
    all_plays = feed.get('liveData', {}).get('plays', {}).get('allPlays', [])
    cur_play  = feed.get('liveData', {}).get('plays', {}).get('currentPlay')

    my_id    = str(PLAYER_ID)
    is_cur   = str(ls.get('defense', {}).get('pitcher', {}).get('id', '')) == my_id
    my_plays = [p for p in all_plays if str(p.get('matchup', {}).get('pitcher', {}).get('id', '')) == my_id]
    my_done  = [p for p in my_plays if p.get('result', {}).get('eventType')]
    outing   = calc_outing(my_done)
    inn      = inn_txt(ls)
    cur_idx  = (cur_play or {}).get('atBatIndex', -1)

    if is_cur and not state['entry_sent']:
        post_slack(wh, msg_entry(game, inn))
        state['entry_sent'] = True
        state['is_pitching'] = True
        print(f'Entry sent — {inn}')

    if is_cur and state['last_ab_idx'] >= 0 and cur_idx != state['last_ab_idx']:
        prev = next((p for p in all_plays if p.get('atBatIndex') == state['last_ab_idx']), None)
        if prev and prev.get('result', {}).get('eventType'):
            post_slack(wh, msg_ab(prev, outing, inn))
            print(f"AB sent — {prev.get('result',{}).get('event')} by {prev.get('matchup',{}).get('batter',{}).get('fullName','?')}")

    if is_cur:
        state['last_ab_idx'] = cur_idx
        state['is_pitching'] = True

    if not is_cur and state['is_pitching'] and my_done:
        post_slack(wh, msg_done(outing, game))
        state['is_pitching'] = False
        print(f"Done sent — {outing['ip']} IP · {outing['k']}K · {outing['r']}R")

    state['date'] = today()

def main():
    wh = os.environ.get('SLACK_WEBHOOK_URL', '')

    if os.environ.get('TEST_MODE', '').lower() == 'true':
        post_slack(wh, {
            'text': '✅ Ritchie Tracker is connected!',
            'blocks': [
                {'type': 'header', 'text': {'type': 'plain_text', 'text': '✅  Ritchie Tracker — Test Successful', 'emoji': True}},
                {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'GitHub Actions poller is running and your Slack webhook is connected.\n\nYou\'ll be notified automatically whenever J.R. Ritchie enters a game.'}},
                {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': 'Polling every 30 sec · noon–midnight ET · ldkobs/ritchie-tracker'}]}
            ]
        })
        print('Test message sent.')
        return

    if os.environ.get('TEST_GAME', '').lower() == 'true':
        fake_game = {'teams': {'away': {'team': {'name': 'Milwaukee Brewers'}, 'score': 2},
                               'home': {'team': {'name': 'Atlanta Braves'},    'score': 3}}}
        fake_plays = [
            {'result': {'eventType': 'strikeout',   'event': 'Strikeout'},   'matchup': {'batter': {'fullName': 'C. Yelich'}}},
            {'result': {'eventType': 'single',      'event': 'Single'},      'matchup': {'batter': {'fullName': 'W. Contreras'}}},
            {'result': {'eventType': 'intent_walk', 'event': 'Intent Walk'}, 'matchup': {'batter': {'fullName': 'S. Wiemer'}}},
        ]
        outings = [
            dict(ip='0.1', k=1, bb=0, h=0, r=0),
            dict(ip='0.2', k=1, bb=0, h=1, r=0),
            dict(ip='0.2', k=1, bb=1, h=1, r=0),
        ]
        final_outing = dict(ip='1.0', k=2, bb=1, h=1, r=0)

        print('Sending entry…')
        post_slack(wh, msg_entry(fake_game, '▼7'))
        time.sleep(8)

        for play, outing in zip(fake_plays, outings):
            print(f"Sending AB: {play['result']['event']}…")
            post_slack(wh, msg_ab(play, outing, '▼7'))
            time.sleep(8)

        print('Sending outing complete…')
        post_slack(wh, msg_done(final_outing, fake_game))
        print('Simulation done.')
        return

    state = load_state()
    start = time.time()

    while True:
        poll_once(state, wh)
        elapsed = time.time() - start
        if elapsed + POLL_INTERVAL >= LOOP_DURATION:
            break
        print(f'Sleeping 30s ({int(LOOP_DURATION - elapsed)}s remaining)…')
        time.sleep(POLL_INTERVAL)

    save_state(state)

if __name__ == '__main__':
    main()
