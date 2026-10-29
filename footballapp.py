from datetime import datetime
import requests
import streamlit as st

# ページの基本設定
st.set_page_config(
    page_title="欧州サッカー トラッカー", page_icon="⚽", layout="centered"
)

API_KEY = "19a1ed45297d4d7dbed19bcf7be5abf4"

# 選択できるリーグの定義（ラ・リーガの表記を修正）
LEAGUES = {
    "プレミアリーグ": "PL",
    "ラ・リーガ": "PD",
    "ブンデスリーガ": "BL1",
    "リーグアン": "FL1",
    "エールディヴィジ": "DED",
}

st.title("⚽ 欧州サッカー トラッカー")

# タブの作成（日程・結果 と 順位表）
tab1, tab2 = st.tabs(["🗓️ 日程・結果", "📊 順位表"])

# --- タブ1: 日程・結果 ---
with tab1:
    st.subheader("試合スケジュール＆結果")

    col1, col2 = st.columns(2)
    with col1:
        selected_league_name = st.selectbox(
            "リーグを選択", list(LEAGUES.keys()), key="schedule_league"
        )
        competition_code = LEAGUES[selected_league_name]

    with col2:
        matchday = st.number_input(
            "節 (Matchday)", min_value=1, max_value=38, value=9, step=1
        )

    url = f"https://api.football-data.org/v4/competitions/{competition_code}/matches?matchday={matchday}"
    headers = {"X-Auth-Token": API_KEY}
    response = requests.get(url, headers=headers)

    if response.status_code == 200:
        data = response.json()
        matches = data.get("matches", [])

        if matches:
            for match in matches:
                utc_date = match.get("utcDate")
                home = match["homeTeam"]["name"]
                away = match["awayTeam"]["name"]
                status = match["status"]

                dt_utc = datetime.strptime(utc_date, "%Y-%m-%dT%H:%M:%SZ")
                total_minutes = (dt_utc.hour * 60 + dt_utc.minute) + (9 * 60)
                jst_hour = (total_minutes // 60) % 24
                jst_min = total_minutes % 60

                if dt_utc.hour + 9 >= 24:
                    dt_jst_day = datetime.fromordinal(dt_utc.toordinal() + 1)
                    date_str = f"{dt_jst_day.month:02d}/{dt_jst_day.day:02d}"
                else:
                    date_str = f"{dt_utc.month:02d}/{dt_utc.day:02d}"

                time_str = f"{jst_hour:02d}:{jst_min:02d}"

                if status == "FINISHED":
                    h_score = match["score"]["fullTime"]["home"]
                    a_score = match["score"]["fullTime"]["away"]
                    score_text = f"**{h_score} - {a_score}**"
                    status_text = "（終了）"
                elif status in ["IN_PLAY", "PAUSED"]:
                    h_score = match["score"]["fullTime"]["home"] or 0
                    a_score = match["score"]["fullTime"]["home"] or 0
                    score_text = f"**{h_score} - {a_score}**"
                    status_text = "🔴 **LIVE**"
                else:
                    score_text = f"{time_str}"
                    status_text = ""

                st.markdown(
                    f"📅 `{date_str}` | {home} {score_text} {away} {status_text}"
                )
        else:
            st.info("該当する試合データが見つかりませんでした。")
    else:
        st.error("APIからデータを取得できませんでした。")

# --- タブ2: 順位表 ---
with tab2:
    st.subheader("現在の順位表")
    selected_standings_league = st.selectbox(
        "順位表を見るリーグ", list(LEAGUES.keys()), key="standings_league"
    )
    standings_code = LEAGUES[selected_standings_league]

    standings_url = (
        f"https://api.football-data.org/v4/competitions/{standings_code}/standings"
    )
    s_response = requests.get(standings_url, headers=headers)

    if s_response.status_code == 200:
        s_data = s_response.json()
        standings = s_data.get("standings", [])

        if standings:
            # HOME, AWAY, TOTAL のいずれかのテーブルを探す
            target_table = None
            for st_type in standings:
                if st_type.get("type") == "TOTAL":
                    target_table = st_type.get("table", [])
                    break
            if not target_table and len(standings) > 0:
                target_table = standings[0].get("table", [])

            if target_table:
                table_data = []
                for row in target_table:
                    # 各項目の安全な取得
                    pos = row.get("position", "-")
                    team_name = row.get("team", {}).get("name", "不明")
                    played = row.get("playedGames", 0)
                    won = row.get("won", 0)
                    draw = row.get("draw", 0)
                    lost = row.get("lost", 0)
                    gf = row.get("goalsFor", 0)
                    ga = row.get("goalsAgainst", 0)
                    gd = row.get("goalDifference", 0)
                    pts = row.get("points", 0)

                    table_data.append(
                        {
                            "順位": pos,
                            "チーム": team_name,
                            "試合": played,
                            "勝": won,
                            "分": draw,
                            "負": lost,
                            "得点": gf,
                            "失点": ga,
                            "得失点": gd,
                            "勝ち点": pts,
                        }
                    )
                st.table(table_data)
            else:
                st.info("順位表のテーブルデータが見つかりませんでした。")
        else:
            st.info("順位表データが取得できませんでした。")
    else:
        st.error(
            f"順位表の取得に失敗しました。(ステータスコード:"
            f" {s_response.status_code})"
        )
