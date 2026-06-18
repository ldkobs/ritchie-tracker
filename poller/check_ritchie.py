#!/usr/bin/env python3
import os, json, time, sys, requests
from datetime import datetime, timezone, timedelta

PLAYER_ID      = 702275   # JR Ritchie
TEAM_ID        = '144'    # Atlanta Braves
STATE_FILE     = 'poller/state.json'
MLB            = 'https://statsapi.mlb.com/api/v1'
POLL_INTERVAL  = 30       # seconds between checks
LOOP_DURATION  = 270      # run for 4.5 min so 5-min cron jobs don't overlap

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

def ordinal(n):
    if 10 <= n % 100 <= 20:
        suf = 'th'
    else:
        suf = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f'{n}{suf}'

def innings_pitched_label(plays, pid):
    my_id = str(pid)
    innings = sorted({p.get('about', {}).get('inning')
                      for p in plays
                      if str(p.get('matchup', {}).get('pitcher', {}).get('id', '')) == my_id
                      and p.get('about', {}).get('inning')})
    if not innings:
        return ''
    if len(innings) == 1:
        return ordinal(innings[0])
    return f'{ordinal(innings[0])}–{ordinal(innings[-1])}'

def ab_log_lines(plays, pid):
    """One line per completed at-bat Ritchie faced: emoji + result + batter."""
    my_id = str(pid)
    lines = []
    for p in plays:
        if str(p.get('matchup', {}).get('pitcher', {}).get('id', '')) != my_id:
            continue
        evt = (p.get('result', {}).get('eventType') or '').lower()
        if not evt:
            continue
        name = p.get('result', {}).get('event') or 'Out'
        em = ('🔴' if 'strikeout' in evt
              else '🔵' if evt in ('walk', 'intent_walk')
              else '💣' if evt == 'home_run'
              else '🟡' if evt in ('single', 'double', 'triple')
              else '⚫')
        bat = p.get('matchup', {}).get('batter', {}).get('fullName') or '?'
        lines.append(f"{em} {name} — {bat}")
    return lines

def outing_from_boxscore(feed, pid):
    """Authoritative final line straight from the boxscore — matches the website."""
    my_id = str(pid)
    teams = feed.get('liveData', {}).get('boxscore', {}).get('teams', {})
    for side in ('away', 'home'):
        pitchers = [str(x) for x in teams.get(side, {}).get('pitchers', [])]
        if my_id in pitchers:
            p = teams[side].get('players', {}).get('ID' + my_id, {}).get('stats', {}).get('pitching', {})
            return dict(
                ip = p.get('inningsPitched', '0'),
                k  = p.get('strikeOuts', 0),
                bb = p.get('baseOnBalls', 0),
                h  = p.get('hits', 0),
                r  = p.get('earnedRuns', 0),
            )
    return None

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
        {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': "You'll get his full line when the game ends."}]}
    ]}

def msg_done(outing, game, innings, ab_lines):
    a, h = game['teams']['away'], game['teams']['home']
    fields = [{'type': 'mrkdwn', 'text': f"*Final Line*\n{ol(outing)}"}]
    if innings:
        fields.append({'type': 'mrkdwn', 'text': f"*Innings Pitched*\n{innings}"})
    blocks = [
        {'type': 'header', 'text': {'type': 'plain_text', 'text': '✅  Game Over — J.R. Ritchie', 'emoji': True}},
        {'type': 'section', 'fields': fields},
    ]
    if ab_lines:
        blocks.append({'type': 'divider'})
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn',
            'text': '*Batters Faced*\n' + '\n'.join(ab_lines)}})
    blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn',
        'text': f"{a['team']['name']} {a['score']} – {h['score']} {h['team']['name']} · Final"}]})
    return {'text': f"✅ Ritchie's final — {outing['ip']} IP · {outing['k']}K · {outing['r']}R", 'blocks': blocks}

def blank_game_state():
    return {'entry_sent': False, 'done_sent': False, 'is_pitching': False}

def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        if s.get('date') != today():
            log(f"New day ({today()}), resetting state.")
            raise ValueError('new day')
        s.setdefault('games', {})
        return s
    except Exception:
        return {'date': today(), 'games': {}}

def save_state(s):
    with open(STATE_FILE, 'w') as f:
        json.dump(s, f)

def fetch_feed(game_pk):
    try:
        return requests.get(
            f'https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live',
            timeout=15).json()
    except Exception as e:
        log(f'Feed error: {e}')
        return None

def poll_once(state, wh):
    d     = mlb('schedule', sportId=1, date=today(), hydrate='linescore,team')
    dates = (d or {}).get('dates', [])
    if not dates:
        log(f'No games found for {today()} (empty dates)')
        return
    games = dates[0].get('games', [])

    braves = [g for g in games
              if TEAM_ID in (str(g['teams']['home']['team']['id']),
                             str(g['teams']['away']['team']['id']))]
    if not braves:
        log('No Braves game today.')
        return
    log(f'{len(braves)} Braves game(s) today.')

    my_id = str(PLAYER_ID)

    for game in braves:
        gs  = game_state(game)
        pk  = str(game['gamePk'])
        log(f'Game {pk} — state={gs}')
        if gs not in ('live', 'final'):
            continue

        gst = state['games'].setdefault(pk, blank_game_state())

        # Already fully reported this game — skip the feed fetch
        if gst['done_sent']:
            continue

        feed = fetch_feed(game['gamePk'])
        if not feed:
            continue

        ls         = feed.get('liveData', {}).get('linescore', {})
        all_plays  = feed.get('liveData', {}).get('plays', {}).get('allPlays', [])
        cur_id     = str(ls.get('defense', {}).get('pitcher', {}).get('id', ''))
        is_cur     = cur_id == my_id
        inn        = inn_txt(ls)
        bs_outing  = outing_from_boxscore(feed, PLAYER_ID)
        appeared   = bs_outing is not None or any(
            str(p.get('matchup', {}).get('pitcher', {}).get('id', '')) == my_id for p in all_plays)

        log(f'  is_cur={is_cur} appeared={appeared} entry_sent={gst["entry_sent"]} done_sent={gst["done_sent"]}')

        # Alert 1 — he comes in
        if is_cur and not gst['entry_sent']:
            post_slack(wh, msg_entry(game, inn))
            gst['entry_sent']  = True
            gst['is_pitching'] = True
            log(f'  Entry sent — {inn}')

        if is_cur:
            gst['is_pitching'] = True

        # Alert 2 — game over, send his final line from the boxscore
        if gs == 'final' and appeared and not gst['done_sent']:
            outing   = bs_outing or dict(ip='0', k=0, bb=0, h=0, r=0)
            innings  = innings_pitched_label(all_plays, PLAYER_ID)
            ab_lines = ab_log_lines(all_plays, PLAYER_ID)
            post_slack(wh, msg_done(outing, game, innings, ab_lines))
            gst['done_sent']   = True
            gst['is_pitching'] = False
            log(f"  Done sent — {outing['ip']} IP · {outing['k']}K · {outing['r']}R ({innings})")

    state['date'] = today()

def main():
    wh = os.environ.get('SLACK_WEBHOOK_URL', '')
    log(f'Poller starting — ET date: {today()}, player_id: {PLAYER_ID}, team_id: {TEAM_ID}')

    if os.environ.get('TEST_MODE', '').lower() == 'true':
        post_slack(wh, {
            'text': '✅ Ritchie Tracker is connected!',
            'blocks': [
                {'type': 'header', 'text': {'type': 'plain_text', 'text': '✅  Ritchie Tracker — Test Successful', 'emoji': True}},
                {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'GitHub Actions poller is running and your Slack webhook is connected.\n\nYou\'ll get an alert when Ritchie enters a game, and his full line when the game ends.'}},
                {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': 'Polling every 30 sec · ldkobs/ritchie-tracker'}]}
            ]
        })
        log('Test message sent.')
        return

    if os.environ.get('TEST_GAME', '').lower() == 'true':
        fake_game = {'teams': {'away': {'team': {'name': 'San Francisco Giants'}, 'score': 7},
                               'home': {'team': {'name': 'Atlanta Braves'},      'score': 5}}}
        final_outing = dict(ip='1.1', k=2, bb=1, h=1, r=0)

        log('Sending entry…')
        post_slack(wh, msg_entry(fake_game, '▼8'))
        time.sleep(8)

        sample_abs = ['🔴 Strikeout — C. Yelich', '🟡 Single — W. Contreras',
                      '💣 Home Run — M. Ozuna', '⚫ Groundout — M. Toglia']
        log('Sending game-over final line…')
        post_slack(wh, msg_done(final_outing, fake_game, '8th–9th', sample_abs))
        log('Simulation done.')
        return

    state = load_state()
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
