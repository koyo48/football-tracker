from datetime import datetime, date
import calendar
import requests
import streamlit as st

import os
import hashlib
import re
import unicodedata
from html.parser import HTMLParser
from html import escape
from concurrent.futures import ThreadPoolExecutor
from icalendar import Calendar
from datetime import timezone, timedelta
from streamlit.errors import StreamlitSecretNotFoundError
from urllib.parse import urlparse


class ApiError(Exception):
    """画面に表示してよい、秘密を含まないエラー。"""


def load_api_key():
    try:
        value = st.secrets.get("FOOTBALL_DATA_API_KEY", "")
    except (FileNotFoundError, StreamlitSecretNotFoundError):
        value = ""
    if not isinstance(value, str) or not value.strip():
        value = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    value = value.strip()
    return "" if value == "REPLACE_WITH_NEW_KEY" else value


@st.cache_data(ttl=60, max_entries=128, show_spinner=False)
def fetch_data(code, resource, api_key, matchday=None, date_from=None, date_to=None):
    # キーもキャッシュの識別対象。別のキーの取得結果を再利用しない。
    # 成功結果だけがキャッシュされ、例外は呼び出し元で処理する。
    if not api_key:
        raise ApiError(
            "APIキーが未設定です。FOOTBALL_DATA_API_KEY を secrets または環境変数に設定してください。"
        )
    params = {}
    if matchday is not None:
        params["matchday"] = matchday
    if date_from is not None:
        params["dateFrom"] = date_from
        params["dateTo"] = date_to
    try:
        response = requests.get(
            f"https://api.football-data.org/v4/competitions/{code}/{resource}",
            headers={"X-Auth-Token": api_key},
            params=params or None,
            timeout=(5, 15),
        )
    except requests.exceptions.Timeout:
        raise ApiError(
            "通信がタイムアウトしました。時間をおいて再度お試しください。"
        ) from None
    except requests.exceptions.RequestException:
        raise ApiError(
            "通信に失敗しました。インターネット接続を確認し、再度お試しください。"
        ) from None
    if response.status_code != 200:
        messages = {
            401: "APIキーを確認してください。",
            403: "APIキーまたは契約プランの閲覧権限を確認してください。",
            429: "APIの利用回数制限に達しました。しばらく待って再度お試しください。",
        }
        detail = messages.get(
            response.status_code,
            "API側で取得に失敗しました。時間をおいて再度お試しください。",
        )
        raise ApiError(f"{detail}（HTTP {response.status_code}）")
    try:
        data = response.json()
    except ValueError:
        raise ApiError(
            "APIから正しいJSONデータを取得できませんでした。再度お試しください。"
        ) from None
    if not isinstance(data, dict) or not isinstance(data.get(resource), list):
        raise ApiError(
            "APIのデータ形式が想定と異なります。時間をおいて再度お試しください。"
        )
    if any(not isinstance(item, dict) for item in data[resource]):
        raise ApiError("APIのデータ形式が想定と異なります。")
    if resource == "matches":
        for item in data[resource]:
            for field in ("homeTeam", "awayTeam", "score"):
                if item.get(field) is not None and not isinstance(item[field], dict):
                    raise ApiError("APIの試合データ形式が想定と異なります。")
            score = item.get("score") or {}
            if score.get("fullTime") is not None and not isinstance(
                score["fullTime"], dict
            ):
                raise ApiError("APIのスコア形式が想定と異なります。")
    if resource == "standings":
        for group in data[resource]:
            rows = group.get("table", [])
            if not isinstance(rows, list) or any(
                not isinstance(r, dict) or not isinstance(r.get("team", {}), dict)
                for r in rows
            ):
                raise ApiError("APIの順位表形式が想定と異なります。")
    return data


def japan_datetime(value):
    if not isinstance(value, str):
        raise ValueError("invalid date")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=9)))


JST = timezone(timedelta(hours=9))
LEAGUES = {
    "🌎 すべてのリーグ（一括表示）": "ALL",
    "プレミアリーグ": "PL",
    "ラ・リーガ": "PD",
    "セリエA": "SA",
    "ブンデスリーガ": "BL1",
    "リーグアン": "FL1",
    "エールディヴィジ": "DED",
    "チャンピオンズリーグ（CL）": "CL",
    "J1": "J1",
}
NAMES = {v: k for k, v in LEAGUES.items()}
DOMESTIC_CODES = ("PL", "PD", "SA", "BL1", "FL1", "DED")
STAGES = {
    "LEAGUE_STAGE": "リーグフェーズ",
    "GROUP_STAGE": "グループステージ",
    "PLAYOFFS": "プレーオフ",
    "LAST_16": "ラウンド16",
    "QUARTER_FINALS": "準々決勝",
    "SEMI_FINALS": "準決勝",
    "FINAL": "決勝",
    "QUALIFICATION": "予選",
    "QUALIFICATION_ROUND_1": "予選1回戦",
    "QUALIFICATION_ROUND_2": "予選2回戦",
    "QUALIFICATION_ROUND_3": "予選3回戦",
    "REGULAR_SEASON": "リーグ戦",
}


def public_text(url):
    """公開フィード専用。認証キーをJリーグ側へ送らない。"""
    try:
        response = requests.get(url, timeout=(5, 15))
    except requests.exceptions.Timeout:
        raise ApiError(
            "公式カレンダーの通信がタイムアウトしました。再度お試しください。"
        ) from None
    except requests.exceptions.RequestException:
        raise ApiError("公式カレンダーに接続できませんでした。") from None
    if response.status_code != 200:
        raise ApiError(
            f"公式カレンダーを取得できませんでした。（HTTP {response.status_code}）"
        )
    try:
        return response.content.decode("utf-8-sig")
    except UnicodeError:
        raise ApiError("公式データの文字形式が想定と異なります。") from None


class ClubParser(HTMLParser):
    """公開クラブ一覧のリンクと名称だけを読む。記事・画像は取り込まない。"""

    def __init__(self):
        super().__init__()
        self.clubs = {}
        self.active = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            match = re.fullmatch(r"/club/([a-z0-9_-]+)/", attrs.get("href", ""))
            if match and "m-club-list-item__link" in attrs.get("class", "").split():
                self.active = match.group(1)
        if self.active and attrs.get("aria-label"):
            self.clubs[self.active] = attrs["aria-label"]

    def handle_endtag(self, tag):
        if tag == "a":
            self.active = None


def parse_clubs(text):
    parser = ClubParser()
    parser.feed(text)
    # 件数減少を「全クラブ取得成功」と扱わない。
    if len(parser.clubs) != 20:
        raise ApiError(
            "J1の20クラブを確認できませんでした。公式ページの形式変更の可能性があります。"
        )
    return parser.clubs


@st.cache_data(ttl=86400, max_entries=2, show_spinner=False)
def j1_clubs():
    return parse_clubs(public_text("https://www.jleague.jp/j1/club/"))


def parse_j1_calendar(text, club_name):
    try:
        # 公式配信に見られるCR CR LFも通常の改行として解釈する。
        calendar = Calendar.from_ical(text.replace("\r\r\n", "\r\n"))
        if calendar.name != "VCALENDAR":
            raise ValueError("not a calendar")
    except Exception:
        raise ApiError("J1のカレンダー形式が想定と異なります。") from None
    matches, skipped = [], 0
    for event in calendar.walk("VEVENT"):
        summary = unicodedata.normalize("NFKC", str(event.get("SUMMARY", "")))
        # J1とJ1百年構想だけ。ACL・カップ戦・J2/J3は混ぜない。
        if not re.match(r"^\[J1(?:百年構想)?\]", summary):
            continue
        try:
            found = re.fullmatch(
                r"\[(J1(?:百年構想)?)\]:\[([HA])\]VS(.+?)(?:\s*\[キックオフ時間\].*)?",
                summary,
            )
            if not found:
                raise ValueError("unknown summary")
            competition, side, opponent = found.groups()
            start = event.decoded("DTSTART")
            uid = str(event.get("UID", ""))
            if not uid or not isinstance(start, date):
                raise ValueError("missing date or uid")
            if isinstance(start, datetime):
                if start.tzinfo is None:
                    raise ValueError("missing timezone")
                local = start.astimezone(JST)
                local_date, utc_date = (
                    local.date().isoformat(),
                    local.astimezone(timezone.utc).isoformat(),
                )
            else:
                local_date, utc_date = start.isoformat(), None
            matches.append(
                {
                    "id": uid,
                    "utcDate": utc_date,
                    "localDate": local_date,
                    "homeTeam": {
                        "name": club_name if side == "H" else opponent.strip()
                    },
                    "awayTeam": {
                        "name": opponent.strip() if side == "H" else club_name
                    },
                    "status": (
                        "CANCELLED"
                        if str(event.get("STATUS", "")) == "CANCELLED"
                        else "CALENDAR"
                    ),
                    "score": {"fullTime": {"home": None, "away": None}},
                    "league_name": "J1",
                    "competition_code": "J1",
                    "stage": competition,
                    "matchday": None,
                    "_side": side,
                    "_club": club_name,
                }
            )
        except Exception:
            skipped += 1
    if skipped and not matches:
        raise ApiError(
            "J1の試合データを読み取れませんでした。公式配信の形式を確認してください。"
        )
    return matches, skipped


@st.cache_data(ttl=3600, max_entries=100, show_spinner=False)
def j1_club_calendar(slug, club_name, calendar_year):
    text = public_text(
        f"https://www.jleague.jp/api/club-calendar/{slug}/?year={calendar_year}&lang=jp"
    )
    return parse_j1_calendar(text, club_name)


def deduplicate(matches):
    unique = {}
    for match in matches:
        code = match["competition_code"]
        identity = match.get("id")
        if identity is None:
            identity = (
                match.get("utcDate"),
                match["homeTeam"]["name"],
                match["awayTeam"]["name"],
            )
        key = (code, identity)
        if key not in unique:
            unique[key] = dict(
                match,
                homeTeam=dict(match["homeTeam"]),
                awayTeam=dict(match["awayTeam"]),
            )
        if code == "J1":
            # 両クラブのフィードに同じ試合がある。UIDで統合し正式クラブ名を補完。
            current = unique[key]
            if match.get("_side") == "H":
                away = current["awayTeam"]
                current.update(match)
                if away.get("_official"):
                    current["awayTeam"] = away
                current["homeTeam"] = {"name": match["_club"], "_official": True}
            else:
                current["awayTeam"] = {"name": match["_club"], "_official": True}
    return sorted(
        unique.values(),
        key=lambda m: (
            m.get("localDate") or japan_datetime(m["utcDate"]).date().isoformat(),
            m.get("utcDate") or "9999",
            str(m.get("id", "")),
        ),
    )


def get_j1_matches(calendar_year):
    try:
        clubs = j1_clubs()
    except ApiError as exc:
        return [], [f"J1：{exc}"]
    matches, errors = [], []

    # 同時接続を3件に制限。クラブ別の成功結果を1時間再利用する。
    def load(item):
        slug, name = item
        try:
            data, skipped = j1_club_calendar(slug, name, calendar_year)
            return data, (
                [f"J1・{name}：読み取れない試合を{skipped}件除外しました。"]
                if skipped
                else []
            )
        except ApiError as exc:
            return [], [f"J1・{name}：{exc}"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        for data, warnings in pool.map(load, clubs.items()):
            matches.extend(data)
            errors.extend(warnings)
    return deduplicate(matches), errors


def normalize_api_matches(data, code):
    matches, skipped = [], 0
    for item in data["matches"]:
        try:
            local = japan_datetime(item.get("utcDate"))
            home, away = item.get("homeTeam") or {}, item.get("awayTeam") or {}
            matches.append(
                dict(
                    item,
                    homeTeam={
                        "name": home.get("name") or "未定",
                        "crest": home.get("crest"),
                    },
                    awayTeam={
                        "name": away.get("name") or "未定",
                        "crest": away.get("crest"),
                    },
                    localDate=local.date().isoformat(),
                    competition_code=code,
                    league_name=NAMES[code],
                )
            )
        except (ValueError, TypeError, OverflowError):
            skipped += 1
    return matches, skipped


def get_matches(code, matchday, api_key, start=None, end=None, calendar_year=None):
    codes = (
        (
            list(DOMESTIC_CODES)
            if matchday is not None
            else [c for c in NAMES if c != "ALL"]
        )
        if code == "ALL"
        else [code]
    )
    matches, errors = [], []
    for target in codes:
        if target == "J1":
            data, warnings = get_j1_matches(calendar_year)
            matches.extend(data)
            errors.extend(warnings)
            continue
        try:
            # 現シーズンの全試合をリーグごとに取得・キャッシュする。
            # 節を変えても同じキャッシュを再利用できるようにする。
            # 日付は日本時間で後から絞り込む（UTCの日付境界の漏れを防ぐ）。
            data = fetch_data(target, "matches", api_key)
            normalized, skipped = normalize_api_matches(data, target)
            if matchday is not None and target in DOMESTIC_CODES:
                normalized = [
                    match for match in normalized if match.get("matchday") == matchday
                ]
            matches.extend(normalized)
            if skipped:
                errors.append(
                    f"{NAMES[target]}：日時が不正な試合を{skipped}件除外しました。"
                )
        except ApiError as exc:
            errors.append(f"{NAMES[target]}：{exc}")
    if start is not None and end is not None:
        matches = [
            m for m in matches if start.isoformat() <= m["localDate"] <= end.isoformat()
        ]
    return deduplicate(matches), errors


def markdown_text(text):
    # 外部のチーム名がリンクや見出しとして解釈されないようにする。
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", str(text).replace("\n", " "))


def team_crest_html(team):
    url = team.get("crest")
    if not isinstance(url, str):
        return ""

    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "crests.football-data.org":
        return ""

    safe_url = escape(url, quote=True)
    return (
        f'<img src="{safe_url}" alt="" '
        'loading="lazy" '
        'style="width:28px;height:28px;'
        'object-fit:contain;flex-shrink:0;">'
    )


def render_matches(matches, show_league):
    st.markdown(
        '<style>[class*="st-key-match_details_"] [data-testid="stMarkdownContainer"] p '
        "{font-size: 1.25rem; line-height: 1.7;}</style>",
        unsafe_allow_html=True,
    )
    previous_date = None
    for index, match in enumerate(matches):
        if match.get("utcDate"):
            dt = japan_datetime(match["utcDate"])
            time_str = dt.strftime("%H:%M")
        else:
            time_str = "時刻未定"
        match_date = date.fromisoformat(match["localDate"])
        if match_date != previous_date:
            weekday = "月火水木金土日"[match_date.weekday()]
            heading = f"{match_date.month}月{match_date.day}日（{weekday}）"
            # 日付から生成した文字列のみをHTMLに渡す。
            st.markdown(
                f'<div style="text-align:center; margin:0.8rem 0 0.2rem; font-size:1.45rem; font-weight:700; color:#F5F6F7;">{heading}</div>',
                unsafe_allow_html=True,
            )
            previous_date = match_date
        home = markdown_text(match["homeTeam"]["name"])
        away = markdown_text(match["awayTeam"]["name"])
        score = (match.get("score") or {}).get("fullTime") or {}
        h = score.get("home")
        a = score.get("away")
        h, a = ("-" if h is None else h), ("-" if a is None else a)
        status = match.get("status")
        center, note = "vs", ""
        if status == "FINISHED":
            center, note = f"**{h} - {a}**", "（終了）"
        elif status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
            center, note = f"**{h} - {a}**", "🔴 **LIVE**"
        elif status == "CALENDAR":
            note = "（日程のみ・結果配信なし）"
            if match.get("stage") == "J1百年構想":
                note += " J1百年構想"
        elif status in ("POSTPONED", "SUSPENDED", "CANCELLED"):
            note = {
                "POSTPONED": "（延期）",
                "SUSPENDED": "（中断）",
                "CANCELLED": "（中止）",
            }[status]
        status_note = note
        tag = f"[{markdown_text(match['league_name'])}] " if show_league else ""
        if match["competition_code"] == "CL":
            stage = STAGES.get(match.get("stage"), match.get("stage") or "ラウンド未定")
            md = match.get("matchday")
            note += f" {markdown_text(stage)}" + (
                f"／第{md}戦" if md is not None else ""
            )
        with st.container(key=f"match_details_{index}"):
            home_label = escape(str(match["homeTeam"]["name"]))
            away_label = escape(str(match["awayTeam"]["name"]))
            home_crest = team_crest_html(match["homeTeam"])
            away_crest = team_crest_html(match["awayTeam"])
            center_label = escape("VS" if center == "vs" else center.replace("**", ""))
            league_label = (
                f'<div style="font-size:0.85rem;opacity:0.7;margin-bottom:0.25rem;">{escape(match["league_name"])}</div>'
                if show_league
                else ""
            )
            status_label = escape(status_note.replace("**", "").strip())
            # J1の長い注記は中央の狭いスコア欄に入れず、カード全幅の下部に表示。
            is_j1_calendar = match["competition_code"] == "J1" and status == "CALENDAR"
            status_html = (
                '<div style="text-align:center;font-size:0.8rem;line-height:1.4;margin-top:0.3rem;white-space:nowrap;">'
                f"{status_label}</div>"
                if status_label and not is_j1_calendar
                else ""
            )
            footer_html = (
                '<div style="text-align:center;font-size:0.8rem;line-height:1.5;'
                'margin-top:0.55rem;overflow-wrap:anywhere;">'
                f"{status_label}</div>"
                if is_j1_calendar
                else ""
            )
            st.markdown(
                league_label
                + '<div class="match-layout" style="display:grid;grid-template-columns:minmax(0,1fr) 4.5rem minmax(0,1fr);'
                'align-items:center;gap:0.5rem;font-size:1.05rem;line-height:1.5;">'
                '<div class="match-home" style="display:grid;grid-template-columns:3.7rem minmax(0,1fr);align-items:center;gap:0.6rem;min-width:0;">'
                '<span style="text-align:center;font-family:monospace;font-size:0.9rem;font-weight:600;'
                "color:#BBF36E;background:rgba(187,243,110,0.08);border:1px solid rgba(187,243,110,0.22);"
                f'border-radius:0.35rem;padding:0.2rem 0.1rem;">{escape(time_str)}</span>'
                '<span style="display:flex;align-items:center;justify-content:center;'
                'gap:0.65rem;min-width:0;overflow-wrap:anywhere;">'
                f"{home_crest}<span>{home_label}</span></span></div>"
                '<div class="match-center"><div style="text-align:center;font-weight:600;font-size:1rem;'
                'background:rgba(128,128,128,0.14);border-radius:0.45rem;padding:0.15rem 0;">'
                f"{center_label}</div>{status_html}</div>"
                '<div class="match-away" style="display:grid;grid-template-columns:minmax(0,1fr) 3.7rem;align-items:center;gap:0.6rem;min-width:0;">'
                '<span style="display:flex;align-items:center;justify-content:center;'
                'gap:0.65rem;min-width:0;overflow-wrap:anywhere;">'
                f"{away_crest}<span>{away_label}</span></span>"
                '<span aria-hidden="true"></span></div></div>' + footer_html,
                unsafe_allow_html=True,
            )
            round_note = note[len(status_note) :].strip()
            if round_note:
                st.caption(round_note)


def standings_rows(rows):
    columns = {
        "順位": "position",
        "試合": "playedGames",
        "勝": "won",
        "分": "draw",
        "負": "lost",
        "得点": "goalsFor",
        "失点": "goalsAgainst",
        "得失点": "goalDifference",
        "勝ち点": "points",
    }
    result = []
    for row in rows:
        record = {
            "順位": row.get("position", "-"),
            "チーム": row.get("team", {}).get("name", "不明"),
        }
        record.update(
            {
                label: row.get(field, "-")
                for label, field in columns.items()
                if label != "順位"
            }
        )
        result.append(record)
    return result


def closest_matchday(matches, today):
    """未消化の最も近い節。全試合終了後は最後の節を選ぶ。"""
    rounds = {}
    for match in matches:
        number = match.get("matchday")
        raw_date = match.get("localDate")
        if not isinstance(number, int) or not raw_date:
            continue
        try:
            match_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        rounds.setdefault(number, []).append((match_date, match.get("status")))
    if not rounds:
        return 9
    upcoming = [
        (min(d for d, status in games if d >= today), number)
        for number, games in rounds.items()
        if any(d >= today for d, status in games)
    ]
    if upcoming:
        return min(upcoming)[1]
    # シーズンが終了したときは最新の開催節を表示する。
    return max(rounds, key=lambda number: max(d for d, _ in rounds[number]))


def standings_season_label(data):
    """APIのシーズン開始/終了日を使い、タイトル表示用の年を生成する。"""
    season = data.get("season") or {}
    start, end = season.get("startDate"), season.get("endDate")
    if not isinstance(start, str) or not isinstance(end, str):
        return ""
    try:
        start_year, end_year = (
            date.fromisoformat(start).year,
            date.fromisoformat(end).year,
        )
    except ValueError:
        return ""
    if start_year == end_year:
        return f"{start_year}シーズン"
    return f"{start_year}/{str(end_year)[-2:]}シーズン"


def render_standings(rows):
    """PCでは全列、スマホでは主要5列を表示するレスポンシブ順位表。"""
    labels = (
        "順位",
        "チーム",
        "試合",
        "勝",
        "分",
        "負",
        "得点",
        "失点",
        "得失点",
        "勝ち点",
    )
    compact = {"勝", "分", "負", "得点", "失点"}
    head = "".join(
        f'<th scope="col" class="{ "hide-mobile" if label in compact else "" }">{label}</th>'
        for label in labels
    )
    body = []
    for row in standings_rows(rows):
        cells = []
        for label in labels:
            value = row.get(label, "-")
            classes = []
            if label in compact:
                classes.append("hide-mobile")
            if label == "チーム":
                classes.append("team-cell")
            if label == "勝ち点":
                classes.append("points-cell")
            if label == "得失点" and isinstance(value, (int, float)):
                classes.append(
                    "gd-positive" if value > 0 else "gd-negative" if value < 0 else ""
                )
                if value > 0:
                    value = f"+{value}"
            class_attr = (
                f' class="{ " ".join(filter(None, classes)) }"' if classes else ""
            )
            cells.append(f"<td{class_attr}>{escape(str(value))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    st.markdown(
        '<div class="standings-scroll"><table class="custom-standings" aria-label="リーグ順位表">'
        f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'
        '<div class="standings-hint">スマートフォンでは主な項目のみ表示しています。</div>',
        unsafe_allow_html=True,
    )


def main():
    st.set_page_config(
        page_title="サッカー トラッカー", page_icon="⚽", layout="centered"
    )

    st.markdown(
        """
        <style>
        /* 全体背景 */
        .stApp,
        [data-testid="stAppViewContainer"] {
            background: #101114;
            color: #ECEEF0;
        }

        [data-testid="stHeader"] {
            display: none !important;
        }

        .block-container {
            max-width: 1050px;
            padding-top: 2rem;
            padding-bottom: 4rem;
        }

        /* 見出し */
        h1, h2, h3 {
            color: #F5F6F7 !important;
            letter-spacing: -0.025em;
        }

        h3 {
            font-weight: 650 !important;
        }

        /* 説明文 */
        [data-testid="stCaptionContainer"] {
            color: #92969E;
        }

        /* タブ */
        .stTabs [data-baseweb="tab-list"] {
            gap: 1.5rem;
            border-bottom: 1px solid #303238;
        }

        .stTabs [data-baseweb="tab"] {
            background: transparent;
            color: #A1A5AD;
            font-weight: 500;
        }

        .stTabs [aria-selected="true"] {
            color: #BBF36E !important;
        }

        .stTabs [data-baseweb="tab-highlight"] {
            background: #BBF36E;
        }

        /* セレクトボックスと入力欄 */
        [data-baseweb="select"] > div,
        [data-testid="stNumberInput"] input,
        [data-testid="stDateInput"] input {
            background: #202226 !important;
            border-color: #363940 !important;
            color: #ECEEF0 !important;
            border-radius: 8px;
        }

        /* ボタン */
        .stButton > button {
            background: #25282C;
            color: #EDEFF0;
            border: 1px solid #3A3D42;
            border-radius: 9px;
            font-weight: 600;
            transition: all 0.18s ease;
        }

        .stButton > button:hover {
            background: #303530;
            border-color: #BBF36E;
            color: #BBF36E;
        }

        .stButton > button:focus-visible {
            outline: 2px solid #BBF36E;
            outline-offset: 2px;
        }

        
        /* 試合カード */
        [class*="st-key-match_details_"] {
            background: #1C1E22;
            border: 1px solid #2F3237;
            border-radius: 12px;
            padding: 1.15rem 1.25rem;
            margin-bottom: 0.65rem;
            min-height: 76px;
            display: flex;
            flex-direction: column;
            justify-content: center;
            transition: background 0.2s ease,
                        border-color 0.2s ease;
        }


        [class*="st-key-match_details_"]:hover {
            background: #24272B;
            border-color: #494D52;
        }

        /* 順位表 */
        [data-testid="stTable"] {
            border: 1px solid #393D43;
            border-radius: 10px;
            overflow: hidden;
        }

        [data-testid="stTable"] table {
            background: #1C1E22;
            color: #ECEEF0;
        }

        [data-testid="stTable"] th {
            background: #30343A !important;
            color: #F5F6F7 !important;
            font-weight: 650;
        }

        [data-testid="stTable"] td {
            color: #E2E6EA !important;
            border-color: #353940 !important;
        }

        [data-testid="stTable"] tbody tr:nth-child(even) {
            background: #24272C;
        }

        /* スマホとPCで列を切り替える順位表 */
        .standings-scroll { overflow-x: auto; border: 1px solid #393D43; border-radius: 10px; }
        .custom-standings { width: 100%; border-collapse: collapse; background: #1C1E22; font-size: 0.86rem; }
        .custom-standings th { background: #30343A; color: #F5F6F7; font-weight: 650; text-align: right; }
        .custom-standings th:nth-child(2), .custom-standings td:nth-child(2) { text-align: left; }
        .custom-standings th, .custom-standings td { padding: 0.64rem 0.58rem; border-bottom: 1px solid #353940; white-space: nowrap; }
        .custom-standings td { color: #E2E6EA; text-align: right; }
        .custom-standings tbody tr:nth-child(even) { background: #24272C; }
        .custom-standings tbody tr:last-child td { border-bottom: 0; }
        .custom-standings .team-cell { min-width: 185px; white-space: normal; font-weight: 500; }
        .custom-standings .points-cell { font-weight: 750; color: #BBF36E; }
        .custom-standings .gd-positive { color: #9AE0BB; }
        .custom-standings .gd-negative { color: #F0A0A0; }
        .standings-hint { display: none; }
        @media (max-width: 640px) {
            .custom-standings { font-size: 0.79rem; }
            .custom-standings .hide-mobile { display: none; }
            .custom-standings th, .custom-standings td { padding: 0.58rem 0.27rem; }
            .custom-standings .team-cell { min-width: 0; overflow-wrap: anywhere; }
            .custom-standings th:nth-child(2), .custom-standings td:nth-child(2) { width: 48%; }
            .standings-hint { display: block; font-size: 0.76rem; color: #AAB0B8; margin-top: 0.4rem; }
        }

        /* 警告・案内 */
        [data-testid="stAlert"] {
            border-radius: 10px;
        }

        /* 区切り線 */
        hr {
            border-color: #303238;
        }
        
        /* 節選択の文字を中央揃え */
        .st-key-selected_matchday [data-testid="stSelectbox"] input[role="combobox"] {
            text-align: center !important;
        }
        
        
        /* iPhone Safari: リーグ選択欄の内側まで16px以上にして自動ズームを抑える */
        /* iPhoneの入力欄フォーカス時の自動ズーム対策 */
        @media (max-width: 640px) {
            input,
            select,
            textarea,
            [role="combobox"],
            [data-baseweb="select"] input,
            [data-baseweb="select"] div[role="combobox"] {
                font-size: 16px !important;
            }
        }

        /* スマートフォン用の試合カード */
        @media (max-width: 640px) {
            .match-layout {
                display: grid !important;
                grid-template-columns: minmax(0, 1fr) 3rem minmax(0, 1fr) !important;
                grid-template-areas:
                    "time time time"
                    "home center away";
                gap: 0.8rem 0.35rem !important;
                align-items: center;
            }

            .match-home,
            .match-away {
                display: contents !important;
            }

            .match-home > span:first-child {
                grid-area: time;
                justify-self: center;
                width: 4.5rem;
                box-sizing: border-box;
            }

            .match-home > span:nth-child(2) {
                grid-area: home;
            }

            .match-away > span:first-child {
                grid-area: away;
            }

            .match-away > span:last-child {
                display: none;
            }

            .match-home > span:nth-child(2),
            .match-away > span:first-child {
                display: flex !important;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                gap: 0.5rem;
                text-align: center;
                font-size: 0.8rem;
                overflow-wrap: break-word !important;
                word-break: normal;
                min-width: 0;
            }

            .match-home img,
            .match-away img {
                width: 30px !important;
                height: 30px !important;
            }

            .match-center {
                grid-area: center;
                align-self: center;
            }
        }
        html, body {
            background-color: #101114 !important;
        }

        @supports (padding: env(safe-area-inset-top)) {
            .stApp {
                min-height: 100dvh;
            }
        }
        
        </style>
        """,
        unsafe_allow_html=True,
    )

    api_key = load_api_key()
    tab1, tab2 = st.tabs(["🗓️ 日程・結果", "📊 順位表"])
    with tab1:
        st.subheader("試合スケジュール＆結果")
        selected = st.selectbox(
            "リーグを選択", list(LEAGUES), index=1, key="schedule_league"
        )
        code = LEAGUES[selected]
        matchday, start, end = None, None, None
        today = datetime.now(JST).date()
        calendar_year = today.year if today.month >= 7 else today.year - 1
        same_matchday = False
        if code == "ALL":
            same_matchday = (
                st.radio(
                    "表示条件",
                    [
                        "期間で表示（欧州6リーグ・CL・J1）",
                        "同じ節で表示（欧州6リーグ）",
                    ],
                )
                == "同じ節で表示（欧州6リーグ）"
            )

        if code in DOMESTIC_CODES or same_matchday:
            # リーグ変更時のみ現在の節へ移動。手動で選んだ節は保持する。
            reference_code = code if code in DOMESTIC_CODES else "PL"
            if st.session_state.get("matchday_reference_code") != reference_code:
                selected_round = 9
                if api_key:
                    try:
                        season_matches = fetch_data(reference_code, "matches", api_key)
                        normalized, _ = normalize_api_matches(
                            season_matches, reference_code
                        )
                        selected_round = closest_matchday(normalized, today)
                    except ApiError:
                        pass  # API障害中は従来の第9節を暫定表示
                st.session_state.selected_matchday = selected_round
                st.session_state.matchday_reference_code = reference_code

            def change_matchday(delta):
                current = st.session_state.selected_matchday
                st.session_state.selected_matchday = max(1, min(38, current + delta))

            st.markdown("**節 (Matchday)**")
            col_prev, col_current, col_next = st.columns(
                [1, 1.3, 1],
                gap="small",
                wrap=False,
            )

            with col_prev:
                st.button(
                    "❮ 前節",
                    disabled=st.session_state.selected_matchday <= 1,
                    use_container_width=True,
                    on_click=change_matchday,
                    args=(-1,),
                )

            with col_current:
                st.selectbox(
                    "節を選択",
                    options=list(range(1, 39)),
                    format_func=lambda x: f"第{x}節",
                    key="selected_matchday",
                    label_visibility="collapsed",
                )

            with col_next:
                st.button(
                    "次節 ❯",
                    disabled=st.session_state.selected_matchday >= 38,
                    use_container_width=True,
                    on_click=change_matchday,
                    args=(1,),
                )

            matchday = st.session_state.selected_matchday

        elif code in ("CL", "J1"):
            # CL/J1は大会全体で共通する節番号がないため、月単位で移動する。
            months = [
                date(year, month, 1)
                for year in range(2025, today.year + 3)
                for month in range(1, 13)
            ]
            if st.session_state.get("selected_schedule_month") not in months:
                st.session_state.selected_schedule_month = today.replace(day=1)

            def change_schedule_month(delta):
                current = st.session_state.selected_schedule_month
                index = months.index(current)
                st.session_state.selected_schedule_month = months[
                    max(0, min(len(months) - 1, index + delta))
                ]

            st.markdown("**表示する月**")
            col_prev, col_current, col_next = st.columns(
                [1, 1.3, 1], gap="small", wrap=False
            )
            with col_prev:
                st.button(
                    "❮ 前月",
                    key="prev_schedule_month",
                    disabled=st.session_state.selected_schedule_month == months[0],
                    use_container_width=True,
                    on_click=change_schedule_month,
                    args=(-1,),
                )
            with col_current:
                st.selectbox(
                    "表示月を選択",
                    options=months,
                    format_func=lambda month: f"{month.year}年{month.month}月",
                    key="selected_schedule_month",
                    label_visibility="collapsed",
                )
            with col_next:
                st.button(
                    "次月 ❯",
                    key="next_schedule_month",
                    disabled=st.session_state.selected_schedule_month == months[-1],
                    use_container_width=True,
                    on_click=change_schedule_month,
                    args=(1,),
                )
            start = st.session_state.selected_schedule_month
            end = date(
                start.year,
                start.month,
                calendar.monthrange(start.year, start.month)[1],
            )

        else:
            st.caption(
                "各大会の節・ラウンドは一致しないため、日本時間の日付で絞り込みます。"
            )
            col1, col2 = st.columns(2)
            with col1:
                start = st.date_input(
                    "開始日（日本時間）", value=today, key="start_date"
                )
            with col2:
                end = st.date_input(
                    "終了日（日本時間）",
                    value=today + timedelta(days=14),
                    key="end_date",
                )
        if code in ("J1", "ALL") and not same_matchday:
            calendar_year = st.number_input(
                "J1公式カレンダー年",
                min_value=2025,
                max_value=today.year + 1,
                value=calendar_year,
                step=1,
            )
            st.caption(
                "J1は公式クラブカレンダーの日程のみ。スコア・順位表・節番号は含まれません。時刻未定はそのまま表示します。"
            )
            st.caption(
                "カレンダー年は公式配信の指定です。2026年版には2026/27シーズンの翌年分も含まれます。過去シーズンの全クラブ収録は保証されません。"
            )
        if start is not None and end < start:
            st.error("終了日は開始日以降を指定してください。")
        else:
            ready = True
            if code == "ALL":
                request_id = (
                    matchday,
                    start,
                    end,
                    calendar_year,
                    hashlib.sha256(api_key.encode()).hexdigest(),
                )
                if st.button("表示", key="show_all_schedule"):
                    with st.spinner("日程を取得しています…"):
                        result = get_matches(
                            code, matchday, api_key, start, end, calendar_year
                        )
                    st.session_state["all_schedule_result"] = (request_id, result)
                saved = st.session_state.get("all_schedule_result")
                ready = saved is not None and saved[0] == request_id
                matches, errors = saved[1] if ready else ([], [])
                if not ready:
                    st.info("条件を選んで「表示」を押してください。")
            else:
                with st.spinner("日程を取得しています…"):
                    matches, errors = get_matches(
                        code, matchday, api_key, start, end, calendar_year
                    )
            if code == "CL" and matches:
                stages = list(
                    dict.fromkeys(m.get("stage") or "UNKNOWN" for m in matches)
                )
                selected_stage = st.selectbox(
                    "ラウンド",
                    ["ALL"] + stages,
                    format_func=lambda x: "すべて" if x == "ALL" else STAGES.get(x, x),
                )
                if selected_stage != "ALL":
                    matches = [
                        m
                        for m in matches
                        if (m.get("stage") or "UNKNOWN") == selected_stage
                    ]
                days = sorted(
                    {
                        m["matchday"]
                        for m in matches
                        if isinstance(m.get("matchday"), int)
                    }
                )
                if days:
                    selected_day = st.selectbox(
                        "節・第何戦",
                        [None] + days,
                        format_func=lambda x: "すべて" if x is None else f"第{x}戦",
                    )
                    if selected_day is not None:
                        matches = [
                            m for m in matches if m.get("matchday") == selected_day
                        ]
            for error in errors:
                st.warning(error)
            if matches:
                render_matches(matches, code == "ALL")
            elif errors:
                st.info(
                    "取得できた範囲では該当する試合を表示できませんでした。取得失敗の案内を確認してください。"
                )
            elif ready:
                st.info("取得元の収録範囲内に、指定条件の試合はありませんでした。")
        if code in ("J1", "ALL") and not same_matchday:
            st.caption(
                "出典：Jリーグ公式クラブカレンダー。変更・結果は公式サイトで確認してください。"
            )
            st.markdown("[J1公式日程・結果](https://www.jleague.jp/j1/match/)")
        st.caption(
            "欧州6リーグ・CL：football-data.org。無料枠では日程・スコアに遅延があります。EL・ECLはこの版の取得対象に含みません。"
        )
    with tab2:
        st.subheader("現在の順位表")
        options = {name: code for name, code in LEAGUES.items() if code != "ALL"}
        selected = st.selectbox(
            "順位表を見るリーグ", list(options), key="standings_league"
        )
        code = options[selected]
        if code == "J1":
            st.info(
                "J1公式カレンダーには順位表が含まれないため、この版では取得できません。"
            )
            st.markdown("[J1公式順位表](https://www.jleague.jp/j1/standings/)")
        elif not api_key:
            st.info(
                "欧州6リーグ・CLの表示には、secrets または環境変数 FOOTBALL_DATA_API_KEY の設定が必要です。J1の日程はキーなしで利用できます。"
            )
        else:
            request_id = (code, hashlib.sha256(api_key.encode()).hexdigest())
            if st.button("順位表を表示", key="show_standings"):
                try:
                    data = fetch_data(code, "standings", api_key)
                    error = None
                except ApiError as exc:
                    data, error = None, str(exc)
                st.session_state["standings_result"] = (request_id, data, error)
            saved = st.session_state.get("standings_result")
            data = None
            if saved is None or saved[0] != request_id:
                st.info("リーグを選んで「順位表を表示」を押してください。")
            elif saved[2]:
                st.error(f"順位表：{saved[2]}")
            else:
                data = saved[1]
            if data is not None:
                tables = [
                    group for group in data["standings"] if group.get("type") == "TOTAL"
                ]
                if not tables:
                    tables = data["standings"][:1]
                found = False
                for group in tables:
                    if group.get("table"):
                        found = True
                        if code == "CL":
                            stage = STAGES.get(
                                group.get("stage"), group.get("stage") or "順位表"
                            )
                            st.caption(f"{stage} {group.get('group') or ''}")
                        season_text = standings_season_label(data)
                        if season_text:
                            st.caption(season_text)
                        render_standings(group["table"])
                if not found:
                    st.info(
                        "取得元に順位表がありません。トーナメント段階にはリーグ形式の順位表がない場合があります。"
                    )


if __name__ == "__main__":
    main()
