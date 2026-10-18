import asyncio
import datetime
import json
import re
import subprocess
import threading
import time
import pytz
import streamlit as st
from playwright.async_api import Playwright, async_playwright, expect

st.set_page_config(page_title="バス発車案内", page_icon="🚌", layout="wide")

JST = pytz.timezone("Asia/Tokyo")

# PC版のダミーデータをそのまま移植（夜間やテスト時にすぐ確認可能）
DUMMY_DATA = [
    {
        "info": 0,
        "line": "45",
        "target_times": ["12:05"],
        "stops": ["書写西住宅", "集会所前", "東洋大姫路前", "田井", "青山", "姫路駅（北口）"],
        "dest_stop": "姫路駅（北口）",
        "destination_times": ["12:31"],
        "status": {"place": "書写西住宅 → 集会所前", "minute": "約3分遅れ"},
        "starting_stop": False,
    },
    {
        "info": 0,
        "line": "41",
        "target_times": ["12:12"],
        "stops": [],
        "dest_stop": "姫路駅（北口）",
        "destination_times": ["12:38"],
        "status": {"place": "六角 → 床坂菅生台", "minute": "正常運行"},
        "starting_stop": False,
    },
    {
        "info": -3,
        "line": "急行",
        "target_times": ["12:20"],
        "stops": [],
        "dest_stop": "姫路駅（北口）",
        "destination_times": ["12:46"],
        "status": {"place": "運行前", "minute": "少し遅れ"},
        "starting_stop": True,
    },
]

# --- PC版のカラーパレットとレイアウトを完全再現するCSS ---
st.markdown(
    """
    <style>
    /* 全体背景をダークモード（#121212）に固定 */
    .stApp {
        background-color: #121212 !important;
        color: #FFFFFF !important;
    }
    header[data-testid="stHeader"] {
        background-color: transparent !important;
    }
    .block-container {
        padding-top: 1rem !important;
        padding-bottom: 1rem !important;
        max-width: 1400px !important;
    }
    /* 更新中に画面が半透明になるのを完全に防ぐ */
    [data-stale="true"], .stFragment [data-stale="true"] {
        opacity: 1 !important;
        transition: none !important;
        filter: none !important;
    }

    /* 右上の小さな通信ステータスバッジ */
    .corner-status {
        position: fixed;
        bottom: 12px;
        right: 16px;
        z-index: 999999;
        background-color: rgba(30, 30, 30, 0.9);
        color: #AAAAAA;
        border: 1px solid #333333;
        padding: 4px 12px;
        border-radius: 4px;
        font-size: 0.8rem;
        pointer-events: none;
    }

    /* ヘッダーバー */
    .signage-header {
        background-color: #181818;
        padding: 12px 24px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-bottom: 2px solid #333333;
        margin-bottom: 12px;
    }
    .route-title {
        font-size: clamp(1.4rem, 3.5vw, 2.8rem);
        font-weight: bold;
        color: #FFFFFF;
    }
    .clock-box {
        display: flex;
        align-items: baseline;
        gap: 12px;
    }
    .clock-label {
        font-size: clamp(0.9rem, 1.8vw, 1.4rem);
        color: #AAAAAA;
    }
    .clock-time {
        font-size: clamp(1.8rem, 4vw, 3.4rem);
        font-weight: bold;
        color: #FFFFFF;
        font-family: 'Frutiger', 'Segoe UI', Roboto, sans-serif;
    }

    /* カラム見出し（時刻・行先・遅れ） */
    .col-headers {
        display: grid;
        grid-template-columns: 18% 12% 38% 20% 12%;
        color: #AAAAAA;
        font-size: clamp(0.9rem, 1.8vw, 1.4rem);
        font-weight: bold;
        padding: 4px 16px;
        text-align: center;
    }
    .col-headers .hdr-dest {
        text-align: left;
        padding-left: 12px;
    }

    /* 第1便・第2便共通のメインカード行 */
    .bus-row-card {
        background-color: #252526;
        display: grid;
        grid-template-columns: 18% 12% 38% 20% 12%;
        align-items: center;
        padding: 10px 16px;
        margin-bottom: 4px;
        min-height: 84px;
    }
    .dep-time {
        font-size: clamp(1.6rem, 3.8vw, 3.2rem);
        font-weight: bold;
        text-align: center;
        font-family: 'Frutiger', 'Segoe UI', sans-serif;
    }
    .line-box {
        display: inline-flex;
        justify-content: center;
        align-items: center;
        width: clamp(48px, 7vw, 80px);
        height: clamp(48px, 7vw, 80px);
        font-size: clamp(1.3rem, 3vw, 2.6rem);
        font-weight: bold;
        color: #FFFFFF;
        margin: 0 auto;
    }
    .dest-name {
        font-size: clamp(1.3rem, 3.2vw, 2.6rem);
        font-weight: bold;
        padding-left: 12px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .delay-text {
        font-size: clamp(1.1rem, 2.6vw, 2.2rem);
        font-weight: bold;
        text-align: center;
    }
    .remark-text {
        font-size: clamp(1.1rem, 2.5vw, 2.1rem);
        font-weight: bold;
        text-align: center;
    }

    /* 第1便の詳細エリア（走行位置） */
    .detail-location-bar {
        background-color: #1E1E1E;
        display: flex;
        align-items: center;
        padding: 10px 16px;
        margin-bottom: 2px;
        gap: 16px;
    }
    .detail-tag {
        color: #AAAAAA;
        font-size: clamp(0.85rem, 1.6vw, 1.2rem);
        white-space: nowrap;
        min-width: 90px;
    }
    .detail-location-val {
        color: #FFFFFF;
        font-size: clamp(1.1rem, 2.4vw, 1.9rem);
        font-weight: bold;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    /* 第1便の下段（到着まで＋停車バス停 ＆ 見込み時刻ルート図） */
    .detail-bottom-grid {
        background-color: #1E1E1E;
        display: grid;
        grid-template-columns: 58% 42%;
        padding: 12px 16px;
        align-items: center;
        margin-bottom: 16px;
    }
    @media (max-width: 768px) {
        .detail-bottom-grid {
            grid-template-columns: 1fr;
            gap: 12px;
        }
    }
    .arrival-countdown {
        font-size: clamp(1.2rem, 2.5vw, 2.0rem);
        font-weight: bold;
        margin-bottom: 10px;
        display: inline-block;
        padding: 2px 10px;
    }
    /* まもなく到着の点滅アニメーション */
    @keyframes blink-warn {
        0%, 49% { background-color: #FF9800; color: #000000; }
        50%, 100% { background-color: transparent; color: #FF9800; }
    }
    .blink-soon {
        animation: blink-warn 2s infinite;
    }

    /* 停車バス停の横スクロール */
    .stops-row {
        display: flex;
        align-items: center;
        gap: 12px;
        overflow: hidden;
    }
    .stops-scroll-wrapper {
        overflow-x: auto;
        white-space: nowrap;
        scrollbar-width: none;
        flex: 1;
    }
    .stops-scroll-wrapper::-webkit-scrollbar {
        display: none;
    }
    @keyframes marquee {
        0%   { transform: translateX(0%); }
        15%  { transform: translateX(0%); }
        85%  { transform: translateX(-65%); }
        100% { transform: translateX(0%); }
    }
    .stops-marquee {
        display: inline-block;
        font-size: clamp(1.0rem, 2.1vw, 1.6rem);
        color: #FFFFFF;
        animation: marquee 18s linear infinite;
    }
    .stops-static {
        display: inline-block;
        font-size: clamp(1.0rem, 2.1vw, 1.6rem);
        color: #FFFFFF;
    }

    /* 右側の出発・到着見込み時刻エリア（縦棒デザイン再現） */
    .est-time-box {
        border-left: 4px solid #FFFFFF;
        padding-left: 16px;
        display: flex;
        align-items: center;
        gap: 14px;
    }
    .est-times-col {
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        height: 96px;
        font-size: clamp(1.3rem, 2.8vw, 2.2rem);
        font-weight: bold;
        font-family: 'Frutiger', 'Segoe UI', sans-serif;
    }
    .route-line-graphic {
        display: flex;
        flex-direction: column;
        align-items: center;
        height: 90px;
        justify-content: space-between;
    }
    .route-dot {
        width: 18px;
        height: 18px;
        background-color: #FFFFFF;
        border-radius: 50%;
    }
    .route-bar {
        width: 5px;
        flex: 1;
        background-color: #FFFFFF;
        margin: -2px 0;
    }
    .est-stops-col {
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        height: 94px;
        font-size: clamp(1.0rem, 2.1vw, 1.6rem);
        font-weight: bold;
        color: #FFFFFF;
        overflow: hidden;
        white-space: nowrap;
    }

    .section-divider {
        height: 2px;
        background-color: #333333;
        margin: 14px 0;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# --- 1. 初回起動時にPlaywrightのChromiumをインストール ---
@st.cache_resource
def install_playwright_browser():
    subprocess.run(["playwright", "install", "chromium"], check=True)


install_playwright_browser()


# --- 2. スクレイピングクラス（元のBusFetcher） ---
class BusFetcher:

    def __init__(self, playwright: Playwright, input_bus_stop, input_end_stop):
        self.playwright = playwright
        self.input_bus_stop = input_bus_stop
        self.input_end_stop = input_end_stop
        self.browser = None
        self.results = []

    async def start(self):
        if not self.browser:
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )

    async def stop(self):
        if self.browser:
            await self.browser.close()
            self.browser = None

    def _init_results(self):
        return [
            {
                "info": 0 if idx == 0 else -9,
                "line": "--",
                "target_times": [],
                "stops": [],
                "dest_stop": "",
                "destination_times": [],
                "status": {},
                "starting_stop": False,
            }
            for idx in range(3)
        ]

    async def run(self):
        if not self.browser:
            await self.start()

        self.results = self._init_results()
        bus_stop = self.input_bus_stop
        end_stop = self.input_end_stop
        search_number = 1

        now_jst = datetime.datetime.now(JST) - datetime.timedelta(minutes=5)
        if 1 <= now_jst.hour < 5:
            search_time = "05:00"
        else:
            search_time = now_jst.strftime("%H:%M")

        context = await self.browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )

        await context.route(
            "**/*.{png,jpg,jpeg,svg,gif,webp,woff,woff2,ttf,otf}",
            lambda route: route.abort(),
        )

        page = await context.new_page()

        try:
            try:
                await page.goto("https://navi.shinkibus.jp/", timeout=30000)
            except Exception:
                self.results[0]["info"] = 403
                return self.results

            await page.locator("#que_start_text").fill(bus_stop)
            await page.locator("#que_start_text_auto").get_by_text(
                bus_stop, exact=True
            ).click(timeout=5000)
            await page.locator("#que_end_text").fill(end_stop)
            await page.locator("#que_end_text_auto").get_by_text(
                end_stop, exact=True
            ).click(timeout=5000)
            await page.locator("#time_new_h").select_option(search_time.split(":")[0])
            await page.locator("#time_new_m").select_option(search_time.split(":")[1])

            await page.get_by_role("button", name="検索").click()
            await page.wait_for_load_state("networkidle")

            try:
                await expect(page.locator("div.content-primary")).to_contain_text(
                    "申し訳ございませんが、運行を終了しております。", timeout=500
                )
                self.results[0]["info"] = -2
                return self.results
            except AssertionError:
                pass

            try:
                await page.locator(".res_mid").first.wait_for(
                    state="visible", timeout=5000
                )
            except Exception:
                pass

            line_numbers = await page.locator(".res_mid").all_text_contents()

            def extract_before_newline(text):
                idx = text.find("\n")
                return text[:idx] if idx != -1 else text

            line_number_list = [
                extract_before_newline(line.strip())
                for line in line_numbers
                if line.strip()
            ]
            search_num_max = min(len(line_number_list), 5)
            await asyncio.sleep(0.2)

            for i in range(0, 3):
                while True:
                    if search_number == 1:
                        element = page.locator(
                            "div.kensaku_result.kensaku_kuro.kensaku_result_sel"
                        )
                        try:
                            await expect(element).to_contain_text(
                                "出発済", timeout=1000
                            )
                            search_number += 1
                            self.results[i]["info"] = -9
                            continue
                        except AssertionError:
                            break

                    elif search_number <= search_num_max:
                        await page.locator(
                            f"div:nth-child({search_number+1}) > .kensaku_res > div > div:nth-child(2) > .saimoku_container > .saimoku"
                        ).click()
                        try:
                            await expect(
                                page.locator(
                                    "div.kensaku_result.kensaku_result_sel.kensaku_kuro"
                                )
                            ).to_contain_text("出発済", timeout=1000)
                            search_number += 1
                            continue
                        except AssertionError:
                            break
                    else:
                        self.results[i]["info"] = -9
                        return self.results

                line = (
                    line_number_list[search_number - 1]
                    if line_number_list and 1 <= search_number <= len(line_number_list)
                    else "00"
                )

                prev_tt_text = await page.locator(".tt").all_text_contents()
                await page.get_by_role(
                    "button", name=re.compile(r"通過 時刻|バス 位置")
                ).click(timeout=3000)

                for _ in range(20):
                    await asyncio.sleep(0.2)
                    current_tt_text = await page.locator(".tt").all_text_contents()
                    cleaned = [t.strip() for t in current_tt_text if t.strip()]
                    if cleaned and (current_tt_text != prev_tt_text or i == 0):
                        if re.search(r"\d{1,2}:\d{2}|\[\d{1,2}\]", " ".join(cleaned)):
                            break

                await asyncio.sleep(0.3)
                titles = await page.locator(".tt").all_text_contents()

                title_list = [t.strip() for t in titles if t.strip()]
                joined = " ".join(title_list)
                stop_name = title_list[0] if title_list else ""
                times = re.findall(r"\b\d{1,2}:\d{2}\b", joined)
                if not times:
                    mins = re.findall(r"\[(\d{1,2})\]", joined)
                    times = [m.zfill(2) for m in mins]

                bus_info = {"stop": stop_name, "times": times}
                lines = [line.strip() for line in bus_info["stop"].split("\n")]
                lines = [line for line in lines if line and line != "\xa0"]
                time_pattern = re.compile(r"^\d{1,2}:\d{2}$")
                stops = [line for line in lines if not time_pattern.match(line)]

                remove_indices = -1
                remove_strings = ""
                remove_pattern = re.compile(r"正常運行|少し遅れ|約\d+分遅れ")
                new_stops = []
                for idx, stop in enumerate(stops):
                    if remove_pattern.search(stop):
                        remove_indices = idx
                        remove_strings = stop
                    else:
                        new_stops.append(stop)
                stops = new_stops
                if remove_indices != -1 and remove_indices > 0:
                    place = f"{stops[remove_indices - 1]} → {stops[remove_indices]}"
                elif remove_indices == 0 and stops:
                    place = f"{stops[0]} 付近"
                else:
                    place = "運行前"
                status = {"place": place, "minute": remove_strings}

                time_table = [
                    {"stop": stop, "time": t}
                    for stop, t in zip(stops, bus_info["times"])
                ]

                self.results[i]["info"] = 0
                self.results[i]["line"] = line
                self.results[i]["target_times"] = [
                    r["time"] for r in time_table if r["stop"] == bus_stop
                ]
                stops_after = []
                found = False
                for r in time_table:
                    if found:
                        stops_after.append(r["stop"])
                    if r["stop"] == bus_stop:
                        found = True
                self.results[i]["stops"] = stops_after
                self.results[i]["dest_stop"] = (
                    time_table[-1]["stop"] if time_table else ""
                )
                self.results[i]["destination_times"] = [
                    r["time"] for r in time_table if r["stop"] == end_stop
                ]
                self.results[i]["status"] = status
                if time_table and bus_stop == time_table[0]["stop"]:
                    self.results[i]["starting_stop"] = True

                if search_number == len(line_number_list):
                    self.results[i]["info"] = -3
                search_number += 1

        finally:
            await context.close()

        return self.results


# --- 3. ブラウザ管理ワーカー ---
class PlaywrightWorker:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.playwright = None
        self.fetcher = None
        self.lock = threading.Lock()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _execute(self, start_stop, end_stop):
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        if self.fetcher is None:
            self.fetcher = BusFetcher(self.playwright, start_stop, end_stop)
            await self.fetcher.start()
        else:
            self.fetcher.input_bus_stop = start_stop
            self.fetcher.input_end_stop = end_stop
        return await self.fetcher.run()

    def fetch(self, start_stop, end_stop):
        with self.lock:
            future = asyncio.run_coroutine_threadsafe(
                self._execute(start_stop, end_stop), self.loop
            )
            return future.result(timeout=60)


@st.cache_resource
def get_worker():
    return PlaywrightWorker()


# --- 4. PC版の _parse_results ロジックを完全移植 ---
def parse_results_like_pc(results):
    if not results:
        return [], []

    delay = 0
    minute_str = results[0].get("status", {}).get("minute", "")
    if minute_str == "少し遅れ":
        delay = 2
    else:
        m = re.match(r"約(\d+)分遅れ", minute_str)
        if m:
            delay = int(m.group(1))

    dep_time = (
        results[0].get("target_times", [""])[0]
        if results[0].get("target_times")
        else ""
    )
    arr_time = (
        results[0].get("destination_times", [""])[0]
        if results[0].get("destination_times")
        else ""
    )
    pro_dep_time, pro_arr_time = dep_time, arr_time

    if dep_time and delay:
        try:
            t = datetime.datetime.strptime(dep_time, "%H:%M")
            t += datetime.timedelta(minutes=delay)
            pro_dep_time = t.strftime("%H:%M")
        except Exception:
            pass
    if arr_time and delay:
        try:
            t = datetime.datetime.strptime(arr_time, "%H:%M")
            t += datetime.timedelta(minutes=delay)
            pro_arr_time = t.strftime("%H:%M")
        except Exception:
            pass

    top_items = [
        {
            "info": int(results[0].get("info", 0)),
            "line": results[0].get("line", "--"),
            "destination": results[0].get("dest_stop", ""),
            "delay": minute_str,
            "status": results[0].get("status", {}).get("place", ""),
            "dep_time": dep_time,
            "stops": results[0].get("stops", []),
            "pro_dep_time": pro_dep_time,
            "pro_arr_time": pro_arr_time,
            "starting_stop": results[0].get("starting_stop", False),
        }
    ]

    bottom_items = []
    i = 1
    while i < len(results) and int(results[i].get("info", -9)) != -9:
        bottom_items.append(
            {
                "info": int(results[i].get("info", 0)),
                "dep_time": (
                    results[i].get("target_times", [""])[0]
                    if results[i].get("target_times")
                    else ""
                ),
                "line": results[i].get("line", "--"),
                "destination": results[i].get("dest_stop", ""),
                "delay": results[i].get("status", {}).get("minute", ""),
                "starting_stop": results[i].get("starting_stop", False),
            }
        )
        i += 1

    return top_items, bottom_items


# --- 5. 設定バー（折りたたみ可能） ---
if "start_stop" not in st.session_state:
    st.session_state.start_stop = "県立大工学部"
if "end_stop" not in st.session_state:
    st.session_state.end_stop = "姫路駅（北口）"
if "use_dummy" not in st.session_state:
    st.session_state.use_dummy = False

with st.expander("⚙️ 設定・バス停変更・ダミーテスト（タップで開閉）", expanded=False):
    c1, c2, c3, c4 = st.columns([3, 1, 3, 3])
    with c1:
        new_start = st.text_input("検索バス停", value=st.session_state.start_stop)
    with c2:
        st.write("")
        if st.button("🔄 入替", use_container_width=True):
            st.session_state.start_stop, st.session_state.end_stop = (
                st.session_state.end_stop,
                st.session_state.start_stop,
            )
            st.rerun()
    with c3:
        new_end = st.text_input("目的地バス停", value=st.session_state.end_stop)
    with c4:
        st.write("")
        st.session_state.use_dummy = st.toggle(
            "🧪 ダミーデータ表示 (PC版のDキー)", value=st.session_state.use_dummy
        )

    st.session_state.start_stop = new_start
    st.session_state.end_stop = new_end


# --- 6. 電光掲示板UIの自動更新描画（20秒ごと） ---
@st.fragment(run_every="20s")
def render_signage():
    badge = st.empty()
    badge.markdown(
        '<div class="corner-status">🔄 更新中...</div>', unsafe_allow_html=True
    )

    now_jst = datetime.datetime.now(JST)
    t0 = time.perf_counter()

    if st.session_state.use_dummy:
        raw_results = DUMMY_DATA
        elapsed = 0.0
        badge.empty()
    else:
        worker = get_worker()
        try:
            raw_results = worker.fetch(
                st.session_state.start_stop, st.session_state.end_stop
            )
            elapsed = time.perf_counter() - t0
            badge.empty()
        except Exception as e:
            badge.markdown(
                '<div class="corner-status" style="color:#F44336;">⚠️ 取得エラー</div>',
                unsafe_allow_html=True,
            )
            st.error(f"スクレイピングエラー: {e}")
            return

    top_items, bottom_items = parse_results_like_pc(raw_results)

    # 1. 上部ヘッダー（バス停区間 ＆ 現在時刻）
    html_parts = [
        f"""
        <div class="signage-header">
            <div class="route-title">{st.session_state.start_stop} &nbsp;➔&nbsp; {st.session_state.end_stop}</div>
            <div class="clock-box">
                <span class="clock-label">現在時刻</span>
                <span class="clock-time">{now_jst.strftime('%H:%M')}</span>
            </div>
        </div>
        <div class="col-headers">
            <div>時 刻</div>
            <div>系 統</div>
            <div class="hdr-dest">行 先</div>
            <div>遅 れ</div>
            <div>備 考</div>
        </div>
        """
    ]

    # 2. 上段（第1便）の生成
    if top_items:
        item = top_items[0]
        info_code = item.get("info", 0)

        if info_code == 403:
            html_parts.append(
                '<div class="bus-row-card"><div style="grid-column: 1 / -1; font-size: 1.8rem; color: #F44336; text-align: center;">サイトへのアクセスがタイムアウトしました</div></div>'
            )
        elif info_code == -2:
            html_parts.append(
                '<div class="bus-row-card"><div style="grid-column: 1 / -1; font-size: 2.0rem; color: #FFFFFF; text-align: center;">本日の運行は終了しました</div></div>'
            )
        elif info_code != -9 and item.get("line") != "--":
            minute_str = item.get("delay", "")
            rem_color = "#FFFFFF"
            pro_color = "#FFFFFF"
            if minute_str == "正常運行":
                rem_color = "#4CAF50"
            elif minute_str == "少し遅れ":
                rem_color, pro_color = "#FF9800", "#FF9800"
            elif re.match(r"約(\d+)分遅れ", minute_str):
                rem_color, pro_color = "#F44336", "#FF9800"

            line_str = item.get("line", "")
            line_bg = "#FF9800" if line_str in ["直行", "急行"] else "#2196F3"

            remarks = []
            if line_str in ["直行", "急行"]:
                remarks.append(f'<span style="color:#FF9800;">{line_str}</span>')
            if info_code == -3:
                remarks.append('<span style="color:#F44336;">最終</span>')
            if item.get("starting_stop"):
                remarks.append('<span style="color:#FFFFFF;">始発</span>')
            remark_html = " / ".join(remarks)

            # 到着までの分数計算
            arrival_min = 999
            pro_dep = item.get("pro_dep_time", "")
            if pro_dep:
                try:
                    t_dep = datetime.datetime.strptime(pro_dep, "%H:%M")
                    now_naive = now_jst.replace(
                        tzinfo=None, second=0, microsecond=0
                    )
                    if t_dep.hour < 3 and now_naive.hour > 20:
                        t_dep = t_dep.replace(
                            year=now_naive.year,
                            month=now_naive.month,
                            day=now_naive.day + 1,
                        )
                    else:
                        t_dep = t_dep.replace(
                            year=now_naive.year,
                            month=now_naive.month,
                            day=now_naive.day,
                        )
                    arrival_min = int((t_dep - now_naive).total_seconds() // 60) - 1
                except Exception:
                    pass

            if st.session_state.use_dummy:
                # ダミーモード時は「まもなく到着」または「約5分」を分かりやすく表示
                arrival_html = '<div class="arrival-countdown blink-soon">まもなく到着</div>'
            elif 2 < arrival_min < 31:
                arrival_html = f'<div class="arrival-countdown">到着まで約 {arrival_min} 分</div>'
            elif arrival_min <= 2:
                arrival_html = '<div class="arrival-countdown blink-soon">まもなく到着</div>'
            else:
                arrival_html = '<div class="arrival-countdown">&nbsp;</div>'

            stops_str = "、".join([s for s in item.get("stops", []) if s])
            stops_class = "stops-marquee" if len(stops_str) > 14 else "stops-static"

            html_parts.append(
                f"""
                <div class="bus-row-card">
                    <div class="dep-time">{item.get('dep_time', '')}</div>
                    <div><div class="line-box" style="background-color: {line_bg};">{line_str}</div></div>
                    <div class="dest-name">{item.get('destination', '')}</div>
                    <div class="delay-text" style="color: {rem_color};">{minute_str}</div>
                    <div class="remark-text">{remark_html}</div>
                </div>
                <div class="detail-location-bar">
                    <div class="detail-tag">走行位置</div>
                    <div class="detail-location-val">{item.get('status', '')}</div>
                </div>
                <div class="detail-bottom-grid">
                    <div>
                        {arrival_html}
                        <div class="stops-row">
                            <span class="detail-tag">停車バス停</span>
                            <div class="stops-scroll-wrapper">
                                <span class="{stops_class}">{stops_str if stops_str else '―'}</span>
                            </div>
                        </div>
                    </div>
                    <div class="est-time-box">
                        <div class="est-times-col">
                            <div style="color: {pro_color};">{item.get('pro_dep_time', '')}</div>
                            <div style="color: {pro_color};">{item.get('pro_arr_time', '')}</div>
                        </div>
                        <div class="route-line-graphic">
                            <div class="route-dot"></div>
                            <div class="route-bar"></div>
                            <div class="route-dot"></div>
                        </div>
                        <div class="est-stops-col">
                            <div>{st.session_state.start_stop}</div>
                            <div>{st.session_state.end_stop}</div>
                        </div>
                    </div>
                </div>
                """
            )
        else:
            html_parts.append(
                '<div class="bus-row-card"><div style="grid-column: 1 / -1; font-size: 1.6rem; color: #AAAAAA; text-align: center;">現在接近中のバスはありません</div></div>'
            )

    # 3. 区切り線と下段（第2便・第3便）の生成
    if bottom_items:
        html_parts.append('<div class="section-divider"></div>')
        for b_item in bottom_items:
            b_min = b_item.get("delay", "")
            b_color = "#FFFFFF"
            if b_min == "正常運行":
                b_color = "#4CAF50"
            elif b_min == "少し遅れ":
                b_color = "#FF9800"
            elif re.match(r"約(\d+)分遅れ", b_min):
                b_color = "#F44336"

            b_line = b_item.get("line", "")
            b_bg = "#FF9800" if b_line in ["直行", "急行"] else "#2196F3"

            b_remarks = []
            if b_line in ["直行", "急行"]:
                b_remarks.append(f'<span style="color:#FF9800;">{b_line}</span>')
            if b_item.get("info") == -3:
                b_remarks.append('<span style="color:#F44336;">最終</span>')
            if b_item.get("starting_stop"):
                b_remarks.append('<span style="color:#FFFFFF;">始発</span>')
            b_rem_html = " / ".join(b_remarks)

            html_parts.append(
                f"""
                <div class="bus-row-card">
                    <div class="dep-time">{b_item.get('dep_time', '')}</div>
                    <div><div class="line-box" style="background-color: {b_bg};">{b_line}</div></div>
                    <div class="dest-name">{b_item.get('destination', '')}</div>
                    <div class="delay-text" style="color: {b_color};">{b_min}</div>
                    <div class="remark-text">{b_rem_html}</div>
                </div>
                """
            )

    st.markdown("".join(html_parts), unsafe_allow_html=True)

    # フッター（最終更新時刻と手動更新・JSON確認）
    f_col1, f_col2, f_col3 = st.columns([6, 2, 2])
    with f_col1:
        mode_str = "🧪 ダミーデータ表示中" if st.session_state.use_dummy else f"最終取得: {now_jst.strftime('%H:%M:%S')} (所要 {elapsed:.1f}秒)"
        st.caption(f"⏱ {mode_str} ／ 20秒ごとに自動更新")
    with f_col2:
        if st.button("🔄 手動更新", use_container_width=True, key="signage_refresh"):
            st.rerun()
    with f_col3:
        with st.popover("📄 JSON確認", use_container_width=True):
            st.json(raw_results)


render_signage()