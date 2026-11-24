import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import html
from pathlib import Path
import webbrowser
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
STANDINGS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/standings"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")
TOTAL_REG_SEASON_WEEKS = 18

# ESPN seasontype=3 week mapping (Week 4 is the Pro Bowl)
POSTSEASON_ROUNDS = {
    1: "Wild Card Round",
    2: "Divisional Round",
    3: "Conference Championships",
    5: "Super Bowl",
}

NFL_DIVISIONS = {
    "AFC East": ["BUF", "MIA", "NE", "NYJ"],
    "AFC North": ["BAL", "CIN", "CLE", "PIT"],
    "AFC South": ["HOU", "IND", "JAX", "TEN"],
    "AFC West": ["DEN", "KC", "LV", "LAC"],
    "NFC East": ["DAL", "NYG", "PHI", "WSH"],
    "NFC North": ["CHI", "DET", "GB", "MIN"],
    "NFC South": ["ATL", "CAR", "NO", "TB"],
    "NFC West": ["ARI", "LAR", "SF", "SEA"],
}
TEAM_TO_DIV = {abbr: div for div, abbrs in NFL_DIVISIONS.items() for abbr in abbrs}


def create_session():
    """Create a requests Session with automatic retries for transient ESPN API errors."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_json(url, params=None, session=None):
    if session is not None:
        response = session.get(url, params=params, timeout=12)
        response.raise_for_status()
        return response.json()
    with create_session() as s:
        response = s.get(url, params=params, timeout=12)
        response.raise_for_status()
        return response.json()


def fetch_full_season(season_year, existing_week_num=None, existing_data=None, session=None):
    """Fetch all 18 regular-season weeks (seasontype=2) in parallel."""
    season_weeks = {}
    if existing_week_num and existing_data:
        season_weeks[existing_week_num] = existing_data

    weeks_to_fetch = [
        w for w in range(1, TOTAL_REG_SEASON_WEEKS + 1) if w not in season_weeks
    ]

    own_session = session is None
    client = session or create_session()
    try:
        def _get_week(w):
            params = {"dates": season_year, "seasontype": 2, "week": w}
            return w, fetch_json(SCOREBOARD_URL, params=params, session=client)

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(_get_week, w): w for w in weeks_to_fetch}
            for future in as_completed(futures):
                w = futures[future]
                try:
                    week_idx, data = future.result()
                    season_weeks[week_idx] = data
                except requests.RequestException as e:
                    print(f"Warning: Could not fetch Regular Season Week {w}: {e}")
    finally:
        if own_session:
            client.close()

    return dict(sorted(season_weeks.items()))


def fetch_postseason(season_year, existing_round_num=None, existing_data=None, session=None):
    """Fetch Wild Card, Divisional, Conference Championships, and Super Bowl (seasontype=3)."""
    rounds_data = {}
    if existing_round_num in POSTSEASON_ROUNDS and existing_data:
        rounds_data[existing_round_num] = existing_data

    rounds_to_fetch = [r for r in POSTSEASON_ROUNDS if r not in rounds_data]

    own_session = session is None
    client = session or create_session()
    try:
        def _get_round(r):
            params = {"dates": season_year, "seasontype": 3, "week": r}
            return r, fetch_json(SCOREBOARD_URL, params=params, session=client)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(_get_round, r): r for r in rounds_to_fetch}
            for future in as_completed(futures):
                r = futures[future]
                try:
                    round_idx, data = future.result()
                    if data.get("events"):
                        rounds_data[round_idx] = data
                except requests.RequestException as e:
                    print(f"Warning: Could not fetch {POSTSEASON_ROUNDS[r]}: {e}")
    finally:
        if own_session:
            client.close()

    return dict(sorted(rounds_data.items()))


def parse_kickoff(raw_date):
    dt_utc = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    return dt_utc.astimezone(LOCAL_TZ)


def get_tv_network(comp):
    broadcasts = comp.get("broadcasts", [])
    names = []
    for b in broadcasts:
        names.extend(b.get("names", []))
    return ", ".join(dict.fromkeys(names)) if names else "TBD"


def get_team_logo(team_obj):
    if team_obj.get("logo"):
        return team_obj["logo"]
    logos = team_obj.get("logos", [])
    if logos and isinstance(logos, list):
        return logos[0].get("href", "")
    return ""


def get_venue_and_weather(event, comp):
    venue = comp.get("venue", {})
    venue_name = venue.get("fullName", "")
    addr = venue.get("address", {})
    city = addr.get("city", "")
    state_or_country = addr.get("state") or addr.get("country") or ""
    loc_parts = [p for p in (city, state_or_country) if p]
    location_str = ", ".join(loc_parts)

    venue_text = venue_name
    if location_str:
        venue_text = f"{venue_name} ({location_str})" if venue_name else location_str

    weather_text = ""
    if venue.get("indoor"):
        weather_text = "Indoor"
    else:
        weather = event.get("weather", {})
        temp = weather.get("temperature")
        desc = weather.get("displayValue", "")
        if temp is not None and desc:
            weather_text = f"{temp}°F, {desc}"
        elif temp is not None:
            weather_text = f"{temp}°F"
        elif desc:
            weather_text = desc

    return venue_text, weather_text


def get_betting_odds(comp):
    odds_list = comp.get("odds", [])
    if not odds_list:
        return ""
    primary = odds_list[0] or {}
    spread = primary.get("details", "")
    over_under = primary.get("overUnder")
    parts = []
    if spread:
        parts.append(f"Line: {spread}")
    if over_under is not None:
        parts.append(f"O/U: {over_under}")
    return " | ".join(parts)


def get_live_situation(comp, away, home):
    sit = comp.get("situation")
    if not sit:
        return None

    possession_id = str(sit.get("possession", ""))
    down_dist = sit.get("shortDownDistanceText") or sit.get("downDistanceText", "")
    yard_line = sit.get("possessionText", "")
    is_red_zone = bool(sit.get("isRedZone", False))

    away_to = sit.get("awayTimeouts")
    home_to = sit.get("homeTimeouts")
    timeouts_str = ""
    if away_to is not None and home_to is not None:
        timeouts_str = (
            f"TO: {away['team']['abbreviation']} {away_to}, "
            f"{home['team']['abbreviation']} {home_to}"
        )

    sit_parts = [p for p in (down_dist, yard_line) if p]
    return {
        "possession_id": possession_id,
        "text": " • ".join(sit_parts),
        "is_red_zone": is_red_zone,
        "timeouts": timeouts_str,
    }


def get_game_leaders(comp, team_id_to_abbr):
    leaders_out = []
    label_map = {
        "passingYards": "PASS",
        "passingLeader": "PASS",
        "rushingYards": "RUSH",
        "rushingLeader": "RUSH",
        "receivingYards": "REC",
        "receivingLeader": "REC",
    }

    for cat in comp.get("leaders", []):
        cat_name = cat.get("name", "")
        short_label = label_map.get(cat_name) or cat.get("shortDisplayName")
        if short_label not in ("PASS", "RUSH", "REC"):
            continue
        top_list = cat.get("leaders", [])
        if not top_list:
            continue
        top = top_list[0]
        athlete = top.get("athlete", {})
        player_name = athlete.get("shortName") or athlete.get("displayName", "")
        stat_val = top.get("displayValue", "")
        team_id = str(top.get("team", {}).get("id", ""))
        team_abbr = team_id_to_abbr.get(team_id, "")

        if player_name and stat_val:
            abbr_str = f" ({team_abbr})" if team_abbr else ""
            leaders_out.append(f"{short_label}: {player_name}{abbr_str} {stat_val}")

    return leaders_out


def render_team(label, logo_url, css_class="", has_possession=False):
    img_html = (
        f'<img src="{html.escape(logo_url)}" alt="" class="team-logo" loading="lazy">'
        if logo_url
        else ""
    )
    poss_html = '<span class="poss-ball" title="Possession">🏈</span>' if has_possession else ""
    classes = f"team-cell {css_class}".strip()
    return f'<span class="{classes}">{img_html}<span>{html.escape(label)}</span>{poss_html}</span>'


def format_status(event, week_label):
    status_type = event["status"]["type"]
    state = status_type.get("state")  # "pre", "in", or "post"

    if state == "pre" and event.get("date"):
        dt_local = parse_kickoff(event["date"])
        time_str = dt_local.strftime("%I:%M %p %Z").lstrip("0")
        return f"{dt_local.strftime('%m/%d')} - {time_str} ({week_label})"

    detail = status_type.get("shortDetail", "TBD")
    return f"{detail} ({week_label})"


def build_schedule_by_day(data):
    events = sorted(data.get("events", []), key=lambda e: e.get("date", ""))
    games_by_day = defaultdict(list)

    for event in events:
        dt_local = parse_kickoff(event["date"])
        day_header = dt_local.strftime("%A, %b %d, %Y")
        time_str = dt_local.strftime("%I:%M %p %Z").lstrip("0")

        comp = event["competitions"][0]
        competitors = comp["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")

        away_id = str(away["team"].get("id", ""))
        home_id = str(home["team"].get("id", ""))
        team_id_to_abbr = {
            away_id: away["team"].get("abbreviation", ""),
            home_id: home["team"].get("abbreviation", ""),
        }

        away_name = away["team"]["displayName"]
        home_name = home["team"]["displayName"]
        away_logo = get_team_logo(away["team"])
        home_logo = get_team_logo(home["team"])
        away_rec = away.get("records", [{}])[0].get("summary", "")
        home_rec = home.get("records", [{}])[0].get("summary", "")
        tv = get_tv_network(comp)

        away_label = f"{away_name} ({away_rec})" if away_rec else away_name
        home_label = f"{home_name} ({home_rec})" if home_rec else home_name

        state = event["status"]["type"].get("state")
        if state == "pre":
            result = "Scheduled"
            badge_class = "badge-pre"
        elif state == "in":
            detail = event["status"]["type"].get("shortDetail", "Live")
            result = f"{away.get('score', '0')} - {home.get('score', '0')} ({detail})"
            badge_class = "badge-live"
        else:
            detail = event["status"]["type"].get("shortDetail", "Final")
            result = f"{away.get('score', '0')} - {home.get('score', '0')} ({detail})"
            badge_class = "badge-final"

        venue_text, weather_text = get_venue_and_weather(event, comp)
        odds_text = get_betting_odds(comp) if state == "pre" else ""
        situation = get_live_situation(comp, away, home) if state == "in" else None
        leaders = get_game_leaders(comp, team_id_to_abbr) if state in ("in", "post") else []

        games_by_day[day_header].append(
            {
                "time": time_str,
                "away": away_label,
                "away_logo": away_logo,
                "away_has_ball": bool(situation and situation["possession_id"] == away_id),
                "home": home_label,
                "home_logo": home_logo,
                "home_has_ball": bool(situation and situation["possession_id"] == home_id),
                "tv": tv,
                "result": result,
                "badge_class": badge_class,
                "venue": venue_text,
                "weather": weather_text,
                "odds": odds_text,
                "situation": situation,
                "leaders": leaders,
            }
        )

    return games_by_day


def render_game_meta(g):
    """Render venue/weather, odds, live situation, and stat leaders under the matchup."""
    lines = []

    sit = g.get("situation")
    if sit and (sit["text"] or sit["is_red_zone"] or sit["timeouts"]):
        rz_badge = '<span class="rz-badge">RED ZONE</span>' if sit["is_red_zone"] else ""
        sit_text = f'<span class="live-sit">{html.escape(sit["text"])}</span>' if sit["text"] else ""
        to_text = f'<span class="meta-pill">{html.escape(sit["timeouts"])}</span>' if sit["timeouts"] else ""
        lines.append(f'<div class="meta-line">{rz_badge}{sit_text}{to_text}</div>')

    info_pills = []
    if g.get("venue"):
        info_pills.append(f'<span>🏟️ {html.escape(g["venue"])}</span>')
    if g.get("weather"):
        info_pills.append(f'<span class="meta-pill">{html.escape(g["weather"])}</span>')
    if g.get("odds"):
        info_pills.append(f'<span class="odds-pill">{html.escape(g["odds"])}</span>')
    if info_pills:
        lines.append(f'<div class="meta-line">{"".join(info_pills)}</div>')

    if g.get("leaders"):
        leader_items = [
            f'<span class="leader-item">{html.escape(item)}</span>'
            for item in g["leaders"]
        ]
        lines.append(f'<div class="meta-line leaders-line">{"".join(leader_items)}</div>')

    return f'<div class="game-meta">{"".join(lines)}</div>' if lines else ""


def render_games_tables(games_by_day):
    sections = []
    for day, games in games_by_day.items():
        rows = []
        for g in games:
            matchup_html = (
                f'<div class="matchup-inline">'
                f'{render_team(g["away"], g["away_logo"], "road-team", g["away_has_ball"])}'
                f'<span class="at-symbol">@</span>'
                f'{render_team(g["home"], g["home_logo"], "home-team", g["home_has_ball"])}'
                f'</div>'
                f'{render_game_meta(g)}'
            )
            rows.append(
                f"""
                <tr class="game-row">
                    <td class="nowrap col-time">{html.escape(g['time'])}</td>
                    <td class="col-matchup">{matchup_html}</td>
                    <td class="col-tv"><span class="tv-tag">{html.escape(g['tv'])}</span></td>
                    <td class="col-status"><span class="badge {g['badge_class']}">{html.escape(g['result'])}</span></td>
                </tr>
                """
            )
        sections.append(
            f"""
            <div class="day-block">
                <h3>{html.escape(day)}</h3>
                <div class="table-wrap schedule-wrap">
                    <table class="schedule-table">
                        <thead>
                            <tr>
                                <th>Time (PST)</th>
                                <th>Matchup &amp; Game-Day Details (Road @ Home)</th>
                                <th>TV</th>
                                <th>Status / Score</th>
                            </tr>
                        </thead>
                        <tbody>
                            {''.join(rows)}
                        </tbody>
                    </table>
                </div>
            </div>
            """
        )
    return "".join(sections) if sections else "<p>No games found.</p>"


def collect_current_week_teams(data, week_label, teams):
    for event in data.get("events", []):
        comp = event["competitions"][0]
        competitors = comp["competitors"]
        state = event["status"]["type"].get("state")
        status_str = format_status(event, week_label)

        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")

        for team, opp, loc, is_home in [
            (home, away, "vs", True),
            (away, home, "@", False),
        ]:
            name = team["team"]["displayName"]
            record = team.get("records", [{}])[0].get("summary", "0-0")
            score_str = (
                "Upcoming"
                if state == "pre"
                else f"{team.get('score', '0')} - {opp.get('score', '0')}"
            )

            teams[name] = {
                "logo": get_team_logo(team["team"]),
                "record": record,
                "on_bye": False,
                "loc": loc,
                "opp_name": opp["team"]["displayName"],
                "opp_logo": get_team_logo(opp["team"]),
                "is_home": is_home,
                "score": score_str,
                "status": status_str,
                "state": state,
                "prev": None,
            }


def attach_previous_week_scores(prev_data, current_week_label, teams):
    """Attach each team's previous week score and backfill teams on a bye this week."""
    for event in prev_data.get("events", []):
        comp = event["competitions"][0]
        competitors = comp["competitors"]
        state = event["status"]["type"].get("state")

        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")

        for team, opp, loc, is_home in [
            (home, away, "vs", True),
            (away, home, "@", False),
        ]:
            name = team["team"]["displayName"]
            t_score_raw = team.get("score", "0")
            o_score_raw = opp.get("score", "0")

            outcome = ""
            outcome_class = ""
            if state == "post":
                try:
                    t_pts, o_pts = int(t_score_raw), int(o_score_raw)
                    if t_pts > o_pts:
                        outcome, outcome_class = "W", "res-win"
                    elif t_pts < o_pts:
                        outcome, outcome_class = "L", "res-loss"
                    else:
                        outcome, outcome_class = "T", "res-tie"
                except ValueError:
                    pass

            prev_info = {
                "outcome": outcome,
                "outcome_class": outcome_class,
                "score": f"{t_score_raw} - {o_score_raw}",
                "loc": loc,
                "opp_abbr": opp["team"]["abbreviation"],
                "opp_name": opp["team"]["displayName"],
                "opp_logo": get_team_logo(opp["team"]),
                "opp_is_home": not is_home,
            }

            if name not in teams:
                teams[name] = {
                    "logo": get_team_logo(team["team"]),
                    "record": team.get("records", [{}])[0].get("summary", "0-0"),
                    "on_bye": True,
                    "loc": "",
                    "opp_name": "Bye Week",
                    "opp_logo": "",
                    "is_home": None,
                    "score": "—",
                    "status": f"Bye ({current_week_label})",
                    "state": "pre",
                    "prev": prev_info,
                }
            else:
                teams[name]["prev"] = prev_info


def fetch_standings_and_playoffs(season=None, session=None):
    """Fetch division standings and AFC/NFC playoff seeding in a single ESPN API call."""
    params = {"season": season} if season else None
    data = fetch_json(STANDINGS_URL, params=params, session=session)

    divisions = {div: [] for div in NFL_DIVISIONS}
    conferences = {"AFC": [], "NFC": []}

    for conf in data.get("children", []):
        conf_abbr = conf.get("abbreviation", "")
        if conf_abbr not in conferences:
            conf_name = conf.get("name", "")
            conf_abbr = "AFC" if "American" in conf_name else ("NFC" if "National" in conf_name else "")

        for entry in conf.get("standings", {}).get("entries", []):
            team = entry["team"]
            abbr = team.get("abbreviation", "")
            div_name = TEAM_TO_DIV.get(abbr)

            stats_display = {
                s["name"]: s.get("displayValue", "-") for s in entry.get("stats", [])
            }
            stats_val = {
                s["name"]: s.get("value", 0) for s in entry.get("stats", [])
            }

            raw_seed = stats_val.get("playoffSeed")
            seed = int(raw_seed) if raw_seed and float(raw_seed) > 0 else 99

            row = {
                "name": team["displayName"],
                "abbr": abbr,
                "logo": get_team_logo(team),
                "div": div_name or "",
                "seed": seed,
                "w": stats_display.get("wins", "0"),
                "l": stats_display.get("losses", "0"),
                "t": stats_display.get("ties", "0"),
                "pct": stats_display.get("winPercent", ".000"),
                "pf": stats_display.get("pointsFor", "0"),
                "pa": stats_display.get("pointsAgainst", "0"),
                "diff": stats_display.get("pointDifferential", "0"),
                "strk": stats_display.get("streak", "-"),
                "div_sort_key": (
                    float(stats_val.get("winPercent", 0) or 0),
                    -seed,
                    float(stats_val.get("divisionWinPercent", 0) or 0),
                    float(stats_val.get("wins", 0) or 0),
                    float(stats_val.get("pointDifferential", 0) or 0),
                ),
                "sort_key": (
                    float(stats_val.get("winPercent", 0) or 0),
                    float(stats_val.get("wins", 0) or 0),
                    float(stats_val.get("pointDifferential", 0) or 0),
                ),
            }

            if div_name:
                divisions[div_name].append(row)
            if conf_abbr in conferences:
                conferences[conf_abbr].append(row)

    for div_name in divisions:
        divisions[div_name].sort(key=lambda r: r["div_sort_key"], reverse=True)

    for conf_abbr in conferences:
        conferences[conf_abbr].sort(
            key=lambda r: (r["seed"], -r["sort_key"][0], -r["sort_key"][1], -r["sort_key"][2])
        )
        for idx, team_row in enumerate(conferences[conf_abbr], start=1):
            if team_row["seed"] == 99:
                team_row["seed"] = idx

    return divisions, conferences


def render_playoff_picture_section(conferences):
    if not conferences or not any(conferences.values()):
        return ""

    conf_cards = []
    for conf_name in ("AFC", "NFC"):
        entries = conferences.get(conf_name, [])
        if not entries:
            continue

        display_entries = entries[:10]
        rows_html = []

        for r in display_entries:
            seed = r["seed"]
            record_str = f"{r['w']}-{r['l']}" + (f"-{r['t']}" if r["t"] not in ("0", "-") else "")

            if seed == 1:
                tier_badge = '<span class="seed-badge seed-bye">#1 • Bye</span>'
                row_class = "playoff-row-bye"
            elif 2 <= seed <= 4:
                tier_badge = f'<span class="seed-badge seed-div">#{seed} • Div</span>'
                row_class = ""
            elif 5 <= seed <= 7:
                tier_badge = f'<span class="seed-badge seed-wc">#{seed} • WC</span>'
                row_class = ""
            else:
                tier_badge = f'<span class="seed-badge seed-hunt">#{seed} • Hunt</span>'
                row_class = "playoff-row-hunt"

            divider = ""
            if seed == 8:
                divider = (
                    '<tr class="cutoff-row">'
                    '<td colspan="5">▲ Playoff Cutoff (Top 7) — In the Hunt ▼</td>'
                    '</tr>'
                )

            rows_html.append(
                f"""{divider}
                <tr class="{row_class}">
                    <td class="nowrap">{tier_badge}</td>
                    <td>{render_team(r['name'], r['logo'], 'div-leader' if seed <= 4 else '')}</td>
                    <td class="nowrap">{html.escape(record_str)}</td>
                    <td>{html.escape(r['pct'])}</td>
                    <td>{html.escape(r['strk'])}</td>
                </tr>
                """
            )

        projected_html = ""
        if len(entries) >= 7:
            by_seed = {e["seed"]: e for e in entries[:7]}
            if all(s in by_seed for s in range(1, 8)):
                matchup_pairs = [(7, 2), (6, 3), (5, 4)]
                pair_lines = [
                    f'<div class="proj-bye"><strong>#1 Bye:</strong> '
                    f'{render_team(by_seed[1]["name"], by_seed[1]["logo"], "home-team")}</div>'
                ]
                for away_s, home_s in matchup_pairs:
                    a, h = by_seed[away_s], by_seed[home_s]
                    away_label = f"#{away_s} {a['abbr']}"
                    home_label = f"#{home_s} {h['abbr']}"
                    pair_lines.append(
                        f'<div class="proj-game">'
                        f'{render_team(away_label, a["logo"], "road-team")}'
                        f'<span class="at-symbol">@</span>'
                        f'{render_team(home_label, h["logo"], "home-team")}'
                        f'</div>'
                    )
                projected_html = f"""
                <div class="proj-box">
                    <div class="proj-title">Projected {conf_name} Wild Card Matchups</div>
                    <div class="proj-grid">{''.join(pair_lines)}</div>
                </div>
                """

        conf_cards.append(
            f"""
            <div class="table-wrap div-card">
                <div class="div-header">{conf_name} Playoff Picture</div>
                <table>
                    <thead>
                        <tr>
                            <th>Seed</th>
                            <th>Team</th>
                            <th>W-L</th>
                            <th>PCT</th>
                            <th>STRK</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(rows_html)}
                    </tbody>
                </table>
                {projected_html}
            </div>
            """
        )

    return f"""
    <section id="playoff-picture">
        <h2>AFC &amp; NFC Playoff Picture (Seeds 1–7 &amp; In the Hunt)</h2>
        <div class="divisions-grid">
            {''.join(conf_cards)}
        </div>
    </section>
    """


def render_postseason_section(season_year, postseason_schedule):
    if not postseason_schedule:
        return f"""
        <section id="postseason">
            <h2>{season_year} NFL Playoffs &amp; Super Bowl</h2>
            <div class="bye-box">
                Postseason matchups (Wild Card, Divisional, Conference Championships, and Super Bowl)
                will appear here automatically once the NFL playoffs begin in January.
            </div>
        </section>
        """

    blocks = []
    for round_num, games_by_day in postseason_schedule.items():
        round_title = POSTSEASON_ROUNDS.get(round_num, f"Playoff Round {round_num}")
        banner_class = "week-banner sb-banner" if round_num == 5 else "week-banner"
        blocks.append(
            f"""
            <div class="postseason-round-block">
                <div class="{banner_class}">🏆 {html.escape(round_title)}</div>
                {render_games_tables(games_by_day)}
            </div>
            """
        )

    return f"""
    <section id="postseason">
        <h2>{season_year} NFL Playoffs &amp; Super Bowl</h2>
        {''.join(blocks)}
    </section>
    """


def render_standings_section(divisions):
    if not divisions or not any(divisions.values()):
        return ""

    cards = []
    for div_name, rows in divisions.items():
        tr_list = []
        for idx, r in enumerate(rows):
            team_html = render_team(r["name"], r["logo"], "div-leader" if idx == 0 else "")
            record_str = f"{r['w']}-{r['l']}" + (f"-{r['t']}" if r["t"] not in ("0", "-") else "")
            diff_val = r["diff"]
            diff_class = (
                "diff-pos"
                if str(diff_val).startswith("+")
                else ("diff-neg" if str(diff_val).startswith("-") else "")
            )

            tr_list.append(
                f"""
                <tr>
                    <td class="sticky-col">{team_html}</td>
                    <td class="nowrap">{html.escape(record_str)}</td>
                    <td>{html.escape(r['pct'])}</td>
                    <td>{html.escape(r['pf'])}</td>
                    <td>{html.escape(r['pa'])}</td>
                    <td class="{diff_class}">{html.escape(diff_val)}</td>
                    <td>{html.escape(r['strk'])}</td>
                </tr>
                """
            )

        cards.append(
            f"""
            <div class="table-wrap div-card">
                <div class="div-header">{html.escape(div_name)}</div>
                <table>
                    <thead>
                        <tr>
                            <th class="sticky-col">Team</th>
                            <th>W-L</th>
                            <th>PCT</th>
                            <th>PF</th>
                            <th>PA</th>
                            <th>DIFF</th>
                            <th>STRK</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(tr_list)}
                    </tbody>
                </table>
            </div>
            """
        )

    return f"""
    <section id="standings">
        <h2>NFL Standings by Division</h2>
        <div class="divisions-grid">
            {''.join(cards)}
        </div>
    </section>
    """


def render_full_season_section(season_year, season_weeks_schedule, current_reg_week):
    if not season_weeks_schedule:
        return ""

    week_buttons = [
        '<button class="week-btn active" data-week="all" onclick="selectSeasonWeek(\'all\')">All Weeks</button>'
    ]
    week_blocks = []

    for w, games_by_day in season_weeks_schedule.items():
        current_tag = " (Current)" if w == current_reg_week else ""
        week_buttons.append(
            f'<button class="week-btn" data-week="{w}" onclick="selectSeasonWeek(\'{w}\')">Wk {w}</button>'
        )
        week_blocks.append(
            f"""
            <div class="season-week-block" data-week="{w}">
                <div class="week-banner">Week {w}{current_tag}</div>
                {render_games_tables(games_by_day)}
            </div>
            """
        )

    return f"""
    <section id="full-season">
        <h2>{season_year} Full Regular Season Schedule &amp; Scores (Weeks 1–{len(season_weeks_schedule)})</h2>
        <div class="season-controls">
            <input
                type="search"
                id="seasonSearch"
                class="search-bar"
                style="margin-bottom: 0;"
                placeholder="Filter whole season by team (e.g. 49ers)..."
                oninput="filterSeason()"
            />
            <div class="week-pills">
                {''.join(week_buttons)}
            </div>
        </div>
        <div id="seasonContainer">
            {''.join(week_blocks)}
        </div>
    </section>
    """


def generate_html(
    current_label,
    season_year,
    games_by_day,
    bye_teams,
    teams,
    prev_week_label=None,
    prev_games_by_day=None,
    divisions=None,
    conferences=None,
    postseason_schedule=None,
    season_weeks_schedule=None,
    current_reg_week=None,
):
    generated_at = datetime.now(LOCAL_TZ).strftime("%B %d, %Y at %I:%M %p %Z")
    current_schedule_html = render_games_tables(games_by_day)

    has_live_games = any(
        g["badge_class"] == "badge-live"
        for games in games_by_day.values()
        for g in games
    )
    refresh_meta = '<meta http-equiv="refresh" content="60">' if has_live_games else ""

    bye_html = ""
    if bye_teams:
        bye_list = ", ".join(sorted(bye_teams))
        bye_html = f"""
        <div class="bye-box">
            <strong>Bye Week Teams ({len(bye_teams)}):</strong> {html.escape(bye_list)}
        </div>
        """

    prev_week_html = ""
    if prev_week_label and prev_games_by_day:
        prev_week_html = f"""
        <section id="previous-week">
            <h2>Previous Week Scores ({html.escape(prev_week_label)})</h2>
            {render_games_tables(prev_games_by_day)}
        </section>
        """

    playoff_picture_html = render_playoff_picture_section(conferences)
    postseason_html = render_postseason_section(season_year, postseason_schedule)
    standings_html = render_standings_section(divisions)
    full_season_html = render_full_season_section(
        season_year, season_weeks_schedule, current_reg_week
    )

    prev_col_header = f"Prev ({prev_week_label})" if prev_week_label else "Prev Week"

    team_rows = []
    for team_name in sorted(teams):
        info = teams[team_name]
        state_class = {
            "in": "badge-live",
            "post": "badge-final",
        }.get(info["state"], "badge-pre")

        if info["on_bye"]:
            team_html = render_team(team_name, info["logo"], "bye-team")
            opp_html = '<span class="muted">Bye Week</span>'
        else:
            team_class = "home-team" if info["is_home"] else "road-team"
            opp_class = "road-team" if info["is_home"] else "home-team"
            team_html = render_team(team_name, info["logo"], team_class)
            opp_html = (
                f'<div class="matchup-inline">'
                f'<span class="at-symbol">{info["loc"]}</span>'
                f'{render_team(info["opp_name"], info["opp_logo"], opp_class)}'
                f'</div>'
            )

        prev = info.get("prev")
        if prev:
            prev_opp_class = "home-team" if prev["opp_is_home"] else "road-team"
            outcome_span = (
                f'<span class="{prev["outcome_class"]}">{prev["outcome"]}</span> '
                if prev["outcome"]
                else ""
            )
            prev_html = (
                f'<div class="matchup-inline">'
                f'<span class="nowrap">{outcome_span}<strong>{html.escape(prev["score"])}</strong></span>'
                f'<span class="at-symbol">{prev["loc"]}</span>'
                f'{render_team(prev["opp_abbr"], prev["opp_logo"], prev_opp_class)}'
                f'</div>'
            )
        elif prev_week_label:
            prev_html = '<span class="muted">Bye Week</span>'
        else:
            prev_html = '<span class="muted">—</span>'

        team_rows.append(
            f"""
            <tr>
                <td class="sticky-col">{team_html}</td>
                <td class="nowrap">{html.escape(info['record'])}</td>
                <td>{opp_html}</td>
                <td class="nowrap"><strong>{html.escape(info['score'])}</strong></td>
                <td><span class="badge {state_class}">{html.escape(info['status'])}</span></td>
                <td>{prev_html}</td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    {refresh_meta}
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🏈</text></svg>">
    <title>NFL {season_year} Scores, Playoff Picture &amp; Full Season</title>
    <style>
        :root {{
            --bg: #0f172a;
            --card: #1e293b;
            --border: #334155;
            --text: #f1f5f9;
            --muted: #94a3b8;
            --accent: #38bdf8;
            --home-yellow: #fde047;
            --road-lightblue: #7dd3fc;
        }}
        * {{ box-sizing: border-box; }}
        html {{
            scroll-behavior: smooth;
            -webkit-text-size-adjust: 100%;
        }}
        body {{
            margin: 0;
            padding: max(20px, env(safe-area-inset-top))
                     max(20px, env(safe-area-inset-right))
                     max(32px, env(safe-area-inset-bottom))
                     max(20px, env(safe-area-inset-left));
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.5;
            overflow-x: hidden;
        }}
        .container {{
            max-width: 1260px;
            margin: 0 auto;
        }}
        header {{
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: baseline;
            gap: 6px;
            border-bottom: 1px solid var(--border);
            padding-bottom: 14px;
            margin-bottom: 16px;
        }}
        h1 {{ margin: 0; font-size: clamp(1.3rem, 4vw, 1.8rem); color: #ffffff; }}
        .timestamp {{ color: var(--muted); font-size: 0.85rem; }}
        .muted {{ color: var(--muted); }}
        .top-bar {{
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            margin-bottom: 20px;
        }}
        .nav-links {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .nav-links a {{
            color: var(--accent);
            text-decoration: none;
            font-size: 0.86rem;
            font-weight: 500;
            padding: 7px 12px;
            border: 1px solid var(--border);
            border-radius: 8px;
            background: var(--card);
            white-space: nowrap;
            -webkit-tap-highlight-color: transparent;
        }}
        .nav-links a:hover, .nav-links a:active {{ background: #273549; }}
        .legend {{
            display: flex;
            flex-wrap: wrap;
            gap: 14px;
            font-size: 0.88rem;
            color: var(--muted);
        }}
        .legend-dot {{
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-right: 6px;
            vertical-align: middle;
        }}
        .dot-home {{ background: var(--home-yellow); }}
        .dot-road {{ background: var(--road-lightblue); }}
        .matchup-inline {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 6px;
        }}
        .team-cell {{
            display: inline-flex;
            align-items: center;
            gap: 7px;
        }}
        .team-logo {{
            width: 24px;
            height: 24px;
            object-fit: contain;
            flex-shrink: 0;
        }}
        .poss-ball {{ font-size: 0.85rem; margin-left: 2px; }}
        .game-meta {{
            margin-top: 7px;
            display: flex;
            flex-direction: column;
            gap: 5px;
            font-size: 0.8rem;
            color: var(--muted);
        }}
        .meta-line {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 6px 8px;
        }}
        .meta-pill {{
            background: #0f172a;
            border: 1px solid var(--border);
            padding: 2px 7px;
            border-radius: 5px;
            color: #cbd5e1;
        }}
        .odds-pill {{
            background: rgba(56, 189, 248, 0.12);
            border: 1px solid rgba(56, 189, 248, 0.35);
            padding: 2px 7px;
            border-radius: 5px;
            color: #bae6fd;
            font-weight: 500;
        }}
        .rz-badge {{
            background: #dc2626;
            color: #ffffff;
            padding: 2px 7px;
            border-radius: 4px;
            font-weight: 700;
            font-size: 0.72rem;
            letter-spacing: 0.04em;
        }}
        .live-sit {{ color: #fde68a; font-weight: 600; }}
        .leaders-line {{ color: #cbd5e1; gap: 6px 8px; }}
        .leader-item {{
            background: rgba(15, 23, 42, 0.65);
            padding: 2px 7px;
            border-radius: 5px;
            border: 1px solid #283548;
        }}
        .home-team {{ color: var(--home-yellow); font-weight: 600; }}
        .road-team {{ color: var(--road-lightblue); font-weight: 600; }}
        .bye-team {{ color: #e2e8f0; font-weight: 600; }}
        .div-leader {{ font-weight: 700; color: #ffffff; }}
        .at-symbol {{ color: var(--muted); font-weight: 400; }}
        .res-win {{ color: #86efac; font-weight: 700; }}
        .res-loss {{ color: #fca5a5; font-weight: 700; }}
        .res-tie {{ color: #cbd5e1; font-weight: 700; }}
        h2 {{
            margin-top: 32px;
            margin-bottom: 12px;
            font-size: clamp(1.15rem, 3.5vw, 1.35rem);
            color: var(--accent);
        }}
        h3 {{
            margin-top: 16px;
            margin-bottom: 8px;
            font-size: 1rem;
            color: #e2e8f0;
        }}
        .week-banner {{
            margin-top: 22px;
            margin-bottom: 8px;
            padding: 8px 14px;
            background: #162032;
            border-left: 4px solid var(--accent);
            border-radius: 6px;
            font-weight: 700;
            font-size: 1.05rem;
            color: #ffffff;
        }}
        .sb-banner {{
            border-left-color: #facc15;
            background: linear-gradient(90deg, #2b2310, #162032);
            color: #fef08a;
        }}
        .season-controls {{
            display: flex;
            flex-direction: column;
            gap: 12px;
            margin-bottom: 16px;
        }}
        .week-pills {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }}
        .week-btn {{
            min-height: 36px;
            padding: 6px 12px;
            border-radius: 8px;
            border: 1px solid var(--border);
            background: var(--card);
            color: var(--text);
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            -webkit-tap-highlight-color: transparent;
        }}
        .week-btn:hover {{ background: #273549; }}
        .week-btn.active {{
            background: var(--accent);
            color: #0f172a;
            border-color: var(--accent);
        }}
        .table-wrap {{
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 10px;
            margin-bottom: 16px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.93rem;
        }}
        th, td {{
            padding: 11px 13px;
            text-align: left;
            border-bottom: 1px solid var(--border);
            vertical-align: middle;
        }}
        th {{
            background: #0b1220;
            color: var(--muted);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.76rem;
            letter-spacing: 0.04em;
        }}
        tr:last-child td {{ border-bottom: none; }}
        tbody tr:hover {{ background: rgba(56, 189, 248, 0.08); }}
        .nowrap {{ white-space: nowrap; }}
        .tv-tag {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 6px;
            background: #334155;
            color: #f8fafc;
            font-size: 0.8rem;
            font-weight: 600;
        }}
        .badge {{
            display: inline-block;
            padding: 3px 9px;
            border-radius: 999px;
            font-size: 0.82rem;
            font-weight: 600;
            white-space: nowrap;
        }}
        .badge-pre {{ background: #334155; color: #cbd5e1; }}
        .badge-live {{ background: #991b1b; color: #fecaca; }}
        .badge-final {{ background: #14532d; color: #bbf7d0; }}
        .seed-badge {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 6px;
            font-size: 0.76rem;
            font-weight: 700;
            white-space: nowrap;
        }}
        .seed-bye {{ background: #854d0e; color: #fef08a; }}
        .seed-div {{ background: #1e3a8a; color: #bfdbfe; }}
        .seed-wc {{ background: #134e4a; color: #99f6e4; }}
        .seed-hunt {{ background: #334155; color: #cbd5e1; }}
        .playoff-row-bye {{ background: rgba(250, 204, 21, 0.07); }}
        .playoff-row-hunt {{ opacity: 0.88; }}
        .cutoff-row td {{
            background: #0b1220;
            color: #f59e0b;
            font-size: 0.74rem;
            font-weight: 700;
            text-align: center;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            padding: 6px 10px;
            border-top: 2px dashed #f59e0b;
            border-bottom: 1px solid var(--border);
        }}
        .proj-box {{
            padding: 12px 14px;
            background: #162032;
            border-top: 1px solid var(--border);
        }}
        .proj-title {{
            font-size: 0.8rem;
            font-weight: 700;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.04em;
            margin-bottom: 8px;
        }}
        .proj-grid {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px 18px;
            align-items: center;
            font-size: 0.88rem;
        }}
        .proj-bye, .proj-game {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .bye-box {{
            background: var(--card);
            border-left: 4px solid var(--accent);
            padding: 12px 16px;
            border-radius: 6px;
            margin: 16px 0 28px;
            color: #e2e8f0;
        }}
        .divisions-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(min(100%, 460px), 1fr));
            gap: 16px;
        }}
        .div-card {{ margin-bottom: 0; }}
        .div-header {{
            padding: 10px 14px;
            background: #162032;
            border-bottom: 1px solid var(--border);
            font-weight: 700;
            color: #f8fafc;
            font-size: 1rem;
        }}
        .diff-pos {{ color: #86efac; font-weight: 600; }}
        .diff-neg {{ color: #fca5a5; font-weight: 600; }}
        .search-bar {{
            width: 100%;
            max-width: 380px;
            padding: 10px 14px;
            margin-bottom: 12px;
            border-radius: 10px;
            border: 1px solid var(--border);
            background: var(--card);
            color: var(--text);
            font-size: 16px; /* Prevents iOS Safari auto-zoom on focus */
        }}
        .search-bar:focus {{
            outline: 2px solid var(--accent);
            border-color: transparent;
        }}

        /* Mobile / iPhone Portrait & Compact Viewports */
        @media (max-width: 768px) {{
            body {{
                padding: max(14px, env(safe-area-inset-top))
                         max(12px, env(safe-area-inset-right))
                         max(28px, env(safe-area-inset-bottom))
                         max(12px, env(safe-area-inset-left));
            }}
            .nav-links {{
                flex-wrap: nowrap;
                overflow-x: auto;
                -webkit-overflow-scrolling: touch;
                width: 100%;
                padding-bottom: 4px;
                scrollbar-width: none;
            }}
            .nav-links::-webkit-scrollbar {{ display: none; }}
            .search-bar {{ max-width: 100%; }}

            /* Convert schedule tables into stacked mobile game cards */
            .schedule-wrap {{
                background: transparent;
                border: none;
                border-radius: 0;
                overflow: visible;
            }}
            .schedule-table,
            .schedule-table tbody {{
                display: block;
                width: 100%;
            }}
            .schedule-table thead {{
                display: none;
            }}
            .schedule-table tr.game-row {{
                display: grid;
                grid-template-columns: auto 1fr auto;
                grid-template-areas:
                    "time tv status"
                    "matchup matchup matchup";
                align-items: center;
                gap: 8px;
                background: var(--card);
                border: 1px solid var(--border);
                border-radius: 12px;
                padding: 12px;
                margin-bottom: 10px;
            }}
            .schedule-table tr.game-row td {{
                display: block;
                padding: 0;
                border-bottom: none;
            }}
            .schedule-table td.col-time {{
                grid-area: time;
                font-size: 0.82rem;
                color: var(--muted);
                font-weight: 600;
            }}
            .schedule-table td.col-tv {{
                grid-area: tv;
            }}
            .schedule-table td.col-status {{
                grid-area: status;
                justify-self: end;
            }}
            .schedule-table td.col-matchup {{
                grid-area: matchup;
                padding-top: 8px;
                border-top: 1px solid rgba(51, 65, 85, 0.65);
            }}
            .matchup-inline {{
                flex-direction: column;
                align-items: flex-start;
                gap: 5px;
            }}
            .matchup-inline .at-symbol {{
                display: none;
            }}
            .matchup-inline .home-team::before {{
                content: "@ ";
                color: var(--muted);
                font-weight: 400;
            }}
            .team-logo {{
                width: 22px;
                height: 22px;
            }}

            /* Compact data tables & sticky Team column */
            th, td {{
                padding: 9px 10px;
                font-size: 0.85rem;
            }}
            .sticky-col {{
                position: sticky;
                left: 0;
                background: var(--card);
                z-index: 2;
                box-shadow: 2px 0 6px rgba(0, 0, 0, 0.35);
            }}
            th.sticky-col {{
                background: #0b1220;
                z-index: 3;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>NFL {season_year} Scores, Playoff Picture &amp; Standings ({html.escape(current_label)})</h1>
            <div class="timestamp">Updated: {html.escape(generated_at)}</div>
        </header>

        <div class="top-bar">
            <div class="legend">
                <span><span class="legend-dot dot-home"></span><span class="home-team">Home Team</span></span>
                <span><span class="legend-dot dot-road"></span><span class="road-team">Road Team</span></span>
                <span>🏈 Possession (Live)</span>
            </div>
            <nav class="nav-links">
                <a href="#current-week">{html.escape(current_label)}</a>
                {'<a href="#previous-week">' + html.escape(prev_week_label) + '</a>' if prev_week_label else ''}
                <a href="#playoff-picture">Playoff Picture</a>
                <a href="#postseason">Playoffs &amp; Super Bowl</a>
                <a href="#standings">Division Standings</a>
                <a href="#all-teams">All 32 Teams</a>
                <a href="#full-season">Full Season (Wks 1–18)</a>
            </nav>
        </div>

        <section id="current-week">
            <h2>{html.escape(current_label)} Schedule &amp; Scores</h2>
            {current_schedule_html}
            {bye_html}
        </section>

        {prev_week_html}

        {playoff_picture_html}

        {postseason_html}

        {standings_html}

        <section id="all-teams">
            <h2>All 32 NFL Teams: Current &amp; Previous Week Scores</h2>
            <input
                type="search"
                id="teamSearch"
                class="search-bar"
                placeholder="Filter teams (e.g. 49ers, Chiefs)..."
                oninput="filterTeams()"
            />
            <div class="table-wrap">
                <table id="teamsTable">
                    <thead>
                        <tr>
                            <th class="sticky-col">Team</th>
                            <th>Record</th>
                            <th>This Week</th>
                            <th>Score</th>
                            <th>Status</th>
                            <th>{html.escape(prev_col_header)}</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(team_rows)}
                    </tbody>
                </table>
            </div>
        </section>

        {full_season_html}
    </div>

    <script>
        let selectedSeasonWeek = "all";

        function filterTeams() {{
            const query = document.getElementById("teamSearch").value.toLowerCase();
            const rows = document.querySelectorAll("#teamsTable tbody tr");
            rows.forEach(row => {{
                row.style.display = row.innerText.toLowerCase().includes(query) ? "" : "none";
            }});
        }}

        function selectSeasonWeek(week) {{
            selectedSeasonWeek = week;
            document.querySelectorAll(".week-btn").forEach(btn => {{
                btn.classList.toggle("active", btn.dataset.week === week);
            }});
            filterSeason();
        }}

        function filterSeason() {{
            const query = document.getElementById("seasonSearch").value.toLowerCase().trim();
            const weekBlocks = document.querySelectorAll(".season-week-block");

            weekBlocks.forEach(block => {{
                const matchesWeek =
                    selectedSeasonWeek === "all" || block.dataset.week === selectedSeasonWeek;

                if (!matchesWeek) {{
                    block.style.display = "none";
                    return;
                }}

                let weekHasVisibleGames = false;
                const dayBlocks = block.querySelectorAll(".day-block");

                dayBlocks.forEach(day => {{
                    let dayHasVisibleRows = false;
                    const rows = day.querySelectorAll("tbody tr.game-row");

                    rows.forEach(row => {{
                        const matchesQuery = !query || row.innerText.toLowerCase().includes(query);
                        row.style.display = matchesQuery ? "" : "none";
                        if (matchesQuery) dayHasVisibleRows = true;
                    }});

                    day.style.display = dayHasVisibleRows ? "" : "none";
                    if (dayHasVisibleRows) weekHasVisibleGames = true;
                }});

                block.style.display = weekHasVisibleGames ? "" : "none";
            }});
        }}
    </script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(
        description="Generate an HTML report of NFL scores, playoff picture, postseason, and standings."
    )
    parser.add_argument("--week", type=int, help="Highlight a specific regular season week (1-18)")
    parser.add_argument("--season", type=int, help="Season year (defaults to current season)")
    parser.add_argument("-o", "--output", default="nfl_scores.html", help="Output HTML file path")
    parser.add_argument("--open", action="store_true", help="Open the HTML file in your browser")
    args = parser.parse_args()

    params = {}
    if args.week:
        params["week"] = args.week
        params["seasontype"] = 2
    if args.season:
        params["dates"] = args.season

    with create_session() as session:
        print("Fetching current NFL scoreboard...")
        try:
            current_data = fetch_json(SCOREBOARD_URL, params=params or None, session=session)
        except requests.RequestException as e:
            print(f"Error fetching ESPN scoreboard: {e}")
            return

        season_year = args.season or current_data.get("season", {}).get("year", "")
        season_type = current_data.get("season", {}).get("type", 2)
        week_num = current_data.get("week", {}).get("number", 1)

        if season_type == 3:
            current_label = POSTSEASON_ROUNDS.get(week_num, f"Postseason Round {week_num}")
            current_reg_week = None
        else:
            current_label = f"Week {week_num}"
            current_reg_week = week_num

        print(f"Fetching full {season_year} NFL regular season (Weeks 1-{TOTAL_REG_SEASON_WEEKS})...")
        season_weeks_raw = fetch_full_season(
            season_year=season_year,
            existing_week_num=week_num if season_type == 2 else None,
            existing_data=current_data if season_type == 2 else None,
            session=session,
        )

        print(f"Fetching {season_year} NFL Playoffs & Super Bowl (seasontype=3)...")
        postseason_raw = fetch_postseason(
            season_year=season_year,
            existing_round_num=week_num if season_type == 3 else None,
            existing_data=current_data if season_type == 3 else None,
            session=session,
        )

        # Build All-32-Teams table from the current regular-season week (or Week 18 during playoffs)
        teams = {}
        if season_type == 2:
            collect_current_week_teams(current_data, f"Wk {week_num}", teams)
            playing_this_week = set(teams.keys())
            prev_week_num = week_num - 1 if week_num > 1 else None
            prev_week_label = f"Week {prev_week_num}" if prev_week_num else None
            prev_games_by_day = None
            if prev_week_num and prev_week_num in season_weeks_raw:
                prev_data = season_weeks_raw[prev_week_num]
                prev_games_by_day = build_schedule_by_day(prev_data)
                attach_previous_week_scores(prev_data, f"Wk {week_num}", teams)
            bye_teams = set(teams.keys()) - playing_this_week
        else:
            # During playoffs, populate All-32-Teams from Week 18 and Week 17
            wk18 = season_weeks_raw.get(TOTAL_REG_SEASON_WEEKS)
            wk17 = season_weeks_raw.get(TOTAL_REG_SEASON_WEEKS - 1)
            if wk18:
                collect_current_week_teams(wk18, f"Wk {TOTAL_REG_SEASON_WEEKS}", teams)
            if wk17:
                attach_previous_week_scores(wk17, f"Wk {TOTAL_REG_SEASON_WEEKS}", teams)
            prev_week_label = f"Week {TOTAL_REG_SEASON_WEEKS}" if wk18 else None
            prev_games_by_day = build_schedule_by_day(wk18) if wk18 else None
            bye_teams = set()

        print("Fetching NFL division standings & playoff seeding...")
        divisions, conferences = None, None
        try:
            divisions, conferences = fetch_standings_and_playoffs(
                season=season_year, session=session
            )
        except requests.RequestException as e:
            print(f"Warning: Could not fetch standings/playoff picture: {e}")

    games_by_day = build_schedule_by_day(current_data)
    season_weeks_schedule = {
        w: build_schedule_by_day(data) for w, data in season_weeks_raw.items()
    }
    postseason_schedule = {
        r: build_schedule_by_day(data) for r, data in postseason_raw.items()
    }

    html_content = generate_html(
        current_label=current_label,
        season_year=season_year,
        games_by_day=games_by_day,
        bye_teams=bye_teams,
        teams=teams,
        prev_week_label=prev_week_label,
        prev_games_by_day=prev_games_by_day,
        divisions=divisions,
        conferences=conferences,
        postseason_schedule=postseason_schedule,
        season_weeks_schedule=season_weeks_schedule,
        current_reg_week=current_reg_week,
    )

    output_path = Path(args.output).resolve()
    output_path.write_text(html_content, encoding="utf-8")
    print(f"HTML report saved to: {output_path}")

    if args.open:
        webbrowser.open(output_path.as_uri())


if __name__ == "__main__":
    main()