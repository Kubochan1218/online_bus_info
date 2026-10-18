import asyncio
import json
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
import pytz
import streamlit as st
from playwright.async_api import Playwright, async_playwright, expect

st.set_page_config(page_title="神姫バス 運行ナビ", page_icon="🚌", layout="centered")

# タイムゾーン設定（クラウドサーバーでも確実に日本時間で検索するため）
JST = pytz.timezone('Asia/Tokyo')


# --- 1. 初回起動時にPlaywrightのChromiumをインストール ---
@st.cache_resource
def install_playwright_browser():
    subprocess.run(["playwright", "install", "chromium"], check=True)

install_playwright_browser()


# --- 2. 元のスクレイピングクラス（クラウド対応版） ---
class BusFetcher:

    def __init__(self, playwright: Playwright, input_bus_stop, input_end_stop):
        self.playwright = playwright
        self.input_bus_stop = input_bus_stop
        self.input_end_stop = input_end_stop
        self.browser = None
        self.results = []
        self.debug = False

    async def start(self):
        if not self.browser:
            # クラウド(Linux)でメモリクラッシュを防ぐ必須オプションを追加
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )

    async def stop(self):
        if self.browser:
            await self.browser.close()
            self.browser = None

    def _init_results(self):
        """毎回の実行前に結果配列をリセット"""
        return [
            {
                'info': 0 if idx == 0 else -9,
                'line': '--',
                'target_times': [],
                'stops': [],
                'dest_stop': '',
                'destination_times': [],
                'status': {},
                'starting_stop': False,
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

        # 【クラウド対応】JST（日本時間）の5分前を計算
        now_jst = datetime.now(JST) - timedelta(minutes=5)
        if 1 <= now_jst.hour < 5:
            search_time = '05:00'
        else:
            search_time = now_jst.strftime('%H:%M')

        context = await self.browser.new_context()

        # 画像・フォント等の通信遮断（高速化）
        await context.route(
            '**/*.{png,jpg,jpeg,svg,gif,webp,woff,woff2,ttf,otf}',
            lambda route: route.abort(),
        )

        page = await context.new_page()

        try:
            try:
                await page.goto('https://navi.shinkibus.jp/', timeout=30000)
                start_time = time.perf_counter()
            except Exception:
                self.results[0]['info'] = 403
                return self.results

            await page.locator('#que_start_text').fill(bus_stop)
            await page.locator('#que_start_text_auto').get_by_text(bus_stop, exact=True).click()
            await page.locator('#que_end_text').fill(end_stop)
            await page.locator('#que_end_text_auto').get_by_text(end_stop, exact=True).click()
            await page.locator('#time_new_h').select_option(search_time.split(':')[0])
            await page.locator('#time_new_m').select_option(search_time.split(':')[1])

            await page.get_by_role('button', name='検索').click()
            await page.wait_for_load_state('networkidle')

            try:
                await expect(page.locator('div.content-primary')).to_contain_text(
                    '申し訳ございませんが、運行を終了しております。',
                    timeout=100
                )
                self.results[0]['info'] = -2
                return self.results
            except AssertionError:
                pass

            line_numbers = await page.locator('.res_mid').all_text_contents()

            def extract_before_newline(text):
                idx = text.find('\n')
                return text[:idx] if idx != -1 else text

            line_number_list = [
                extract_before_newline(line.strip())
                for line in line_numbers
                if line.strip()
            ]
            search_num_max = min(len(line_number_list), 5)
            await asyncio.sleep(0.15)

            for i in range(0, 3):
                while True:
                    if search_number == 1:
                        element = page.locator('div.kensaku_result.kensaku_kuro.kensaku_result_sel')
                        try:
                            await expect(element).to_contain_text('出発済', timeout=1000)
                            search_number += 1
                            self.results[i]['info'] = -9
                            continue
                        except AssertionError:
                            break

                    elif search_number <= search_num_max:
                        await page.locator(
                            f'div:nth-child({search_number+1}) > .kensaku_res > div > div:nth-child(2) > .saimoku_container > .saimoku'
                        ).click()
                        try:
                            await expect(
                                page.locator('div.kensaku_result.kensaku_result_sel.kensaku_kuro')
                            ).to_contain_text('出発済', timeout=1000)
                            search_number += 1
                            continue
                        except AssertionError:
                            break
                    else:
                        self.results[i]['info'] = -9
                        return self.results

                line = (
                    line_number_list[search_number - 1]
                    if line_number_list and 1 <= search_number <= len(line_number_list)
                    else '00'
                )
                await page.get_by_role('button', name=re.compile(r'通過 時刻|バス 位置')).click(timeout=1500)

                await asyncio.sleep(1)
                titles = await page.locator('.tt').all_text_contents()

                title_list = [t.strip() for t in titles if t.strip()]
                joined = ' '.join(title_list)
                stop_name = title_list[0] if title_list else ''
                times = re.findall(r'\b\d{1,2}:\d{2}\b', joined)
                if not times:
                    mins = re.findall(r'\[(\d{1,2})\]', joined)
                    times = [m.zfill(2) for m in mins]

                bus_info = {'stop': stop_name, 'times': times}
                lines = [line.strip() for line in bus_info['stop'].split('\n')]
                lines = [line for line in lines if line and line != '\xa0']
                time_pattern = re.compile(r'^\d{1,2}:\d{2}$')
                stops = [line for line in lines if not time_pattern.match(line)]

                remove_indices = -1
                remove_strings = ''
                remove_pattern = re.compile(r'正常運行|少し遅れ|約\d+分遅れ')
                new_stops = []
                for idx, stop in enumerate(stops):
                    if remove_pattern.search(stop):
                        remove_indices = idx
                        remove_strings = stop
                    else:
                        new_stops.append(stop)
                stops = new_stops
                if remove_indices != -1:
                    place = f'{stops[remove_indices - 1]} → {stops[remove_indices]}'
                else:
                    place = '運行前'
                status = {'place': place, 'minute': remove_strings}

                time_table = [
                    {'stop': stop, 'time': t}
                    for stop, t in zip(stops, bus_info['times'])
                ]

                self.results[i]['info'] = 0
                self.results[i]['line'] = line
                self.results[i]['target_times'] = [
                    r['time'] for r in time_table if r['stop'] == bus_stop
                ]
                stops_after = []
                found = False
                for r in time_table:
                    if found:
                        stops_after.append(r['stop'])
                    if r['stop'] == bus_stop:
                        found = True
                self.results[i]['stops'] = stops_after
                self.results[i]['dest_stop'] = (
                    time_table[-1]['stop'] if time_table else ''
                )
                self.results[i]['destination_times'] = [
                    r['time'] for r in time_table if r['stop'] == end_stop
                ]
                self.results[i]['status'] = status
                if time_table and bus_stop == time_table[0]['stop']:
                    self.results[i]['starting_stop'] = True

                if search_number == len(line_number_list):
                    self.results[i]['info'] = -3
                search_number += 1

        finally:
            await context.close()

        return self.results


# --- 3. ブラウザを裏スレッドで1度だけ起動して使い回すワーカー ---
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
        # 複数タブから同時アクセスされても順番に安全に処理するロック
        with self.lock:
            future = asyncio.run_coroutine_threadsafe(
                self._execute(start_stop, end_stop), self.loop
            )
            return future.result(timeout=60)


@st.cache_resource
def get_worker():
    return PlaywrightWorker()


# --- 4. スマホ向けUIと自動更新表示（画面が消えない改良版） ---
st.title("🚌 神姫バス 接近情報")

# 【対策1】更新中に画面が半透明（薄暗く）なるのを防ぎ、右上に小さな通信中バッジを出すCSS
st.markdown(
    """
    <style>
    /* 更新中に既存の表示が薄暗く（半透明に）なるのを完全に防ぐ */
    [data-stale="true"] {
        opacity: 1 !important;
        transition: none !important;
    }
    /* 画面右上に固定表示する小さなステータスバッジ */
    .corner-status {
        position: fixed;
        top: 3.5rem;
        right: 1rem;
        z-index: 999999;
        background-color: rgba(30, 30, 30, 0.85);
        color: #ffffff;
        padding: 4px 10px;
        border-radius: 999px;
        font-size: 0.75rem;
        box-shadow: 0 2px 6px rgba(0,0,0,0.2);
        pointer-events: none;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# 乗車・降車バス停の状態管理
if "start_stop" not in st.session_state:
    st.session_state.start_stop = "県立大工学部"
if "end_stop" not in st.session_state:
    st.session_state.end_stop = "姫路駅（北口）"

# 前回の取得結果を保持する変数（更新中に画面が消えるのを防ぐ）
if "last_results" not in st.session_state:
    st.session_state.last_results = None
if "last_updated_str" not in st.session_state:
    st.session_state.last_updated_str = None

col_s1, col_s2, col_s3 = st.columns([4, 1, 4])
with col_s1:
    start_input = st.text_input("乗車バス停", value=st.session_state.start_stop)
with col_s2:
    st.write("")
    if st.button("🔄", help="乗車と降車を入れ替える"):
        st.session_state.start_stop, st.session_state.end_stop = (
            st.session_state.end_stop,
            st.session_state.start_stop,
        )
        st.session_state.last_results = None  # バス停を変えたら表示をリセット
        st.rerun()
with col_s3:
    end_input = st.text_input("降車バス停", value=st.session_state.end_stop)

# 手動でバス停名を書き換えた場合もリセット
if (
    start_input != st.session_state.start_stop
    or end_input != st.session_state.end_stop
):
    st.session_state.start_stop = start_input
    st.session_state.end_stop = end_input
    st.session_state.last_results = None


# バス情報のカードを描画する関数
def draw_bus_cards(results, updated_info):
    st.caption(f"⏱ {updated_info} （20秒ごとに自動更新）")

    first_info = results[0]["info"] if results else -9
    if first_info == 403:
        st.error("⚠️ サイトへのアクセスがタイムアウト、または遮断されました。")
        return
    elif first_info == -2:
        st.warning("🌙 本日の運行は終了しております。")
        return

    valid_buses = [
        b for b in results if b["info"] in (0, -3) and b["line"] != "--"
    ]
    if not valid_buses:
        st.info("現在表示できる接近中のバスはありません。")
    else:
        for idx, bus in enumerate(valid_buses):
            with st.container(border=True):
                dep_time = (
                    bus["target_times"][0] if bus["target_times"] else "--:--"
                )
                arr_time = (
                    bus["destination_times"][0]
                    if bus["destination_times"]
                    else "--:--"
                )
                delay_str = bus["status"].get("minute", "") or "定刻・運行前"
                place_str = bus["status"].get("place", "不明")

                st.subheader(
                    f"第{idx+1}便 | 系統: {bus['line']} （{bus['dest_stop']} 行）"
                )

                c1, c2 = st.columns(2)
                c1.metric(
                    label=f"🚏 {st.session_state.start_stop} 発",
                    value=dep_time,
                    delta=delay_str,
                    delta_color="inverse" if "遅れ" in delay_str else "normal",
                )
                c2.metric(
                    label=f"🏁 {st.session_state.end_stop} 着", value=arr_time
                )

                if place_str == "運行前":
                    st.info("📍 **現在位置:** 運行前（始発待機中）")
                else:
                    st.success(f"📍 **走行位置:** {place_str}")

                if bus["stops"]:
                    with st.expander("停車するバス停一覧を見る"):
                        st.write(" → ".join(bus["stops"]))

    col_btn, col_json = st.columns(2)
    with col_btn:
        if st.button("🔄 今すぐ手動更新", use_container_width=True):
            st.rerun()
    with col_json:
        with st.popover("📄 JSONデータを見る", use_container_width=True):
            st.json(results)


# 20秒ごとに自動更新される領域
@st.fragment(run_every="20s")
def render_bus_schedule():
    # 右上の端に表示する小さな更新中バッジ用の枠
    badge_placeholder = st.empty()
    # メインのバス情報表示枠
    main_placeholder = st.empty()

    # 1. すでに前回のデータがある場合は、通信中もそれを画面に残したままにする！
    if st.session_state.last_results is not None:
        with main_placeholder.container():
            draw_bus_cards(
                st.session_state.last_results, st.session_state.last_updated_str
            )

    # 2. 画面の右上にだけ小さく「🔄 更新中...」と出す（画面は邪魔しない）
    badge_placeholder.markdown(
        '<div class="corner-status">🔄 データ更新中...</div>',
        unsafe_allow_html=True,
    )

    # 3. 裏でスクレイピングを実行
    worker = get_worker()
    t0 = time.perf_counter()
    try:
        results = worker.fetch(
            st.session_state.start_stop, st.session_state.end_stop
        )
        elapsed = time.perf_counter() - t0
        now_str = datetime.now(JST).strftime("%H:%M:%S")

        # 取得成功したらデータを保存
        st.session_state.last_results = results
        st.session_state.last_updated_str = (
            f"最終更新: {now_str} （所要時間: {elapsed:.1f}秒）"
        )
    except Exception as e:
        badge_placeholder.markdown(
            '<div class="corner-status" style="background-color: #d9534f;">⚠️ 更新失敗（次回再試行）</div>',
            unsafe_allow_html=True,
        )
        if st.session_state.last_results is None:
            st.error(f"初回取得エラー: {e}")
        return

    # 4. 取得が終わったら右上のバッジを消し、最新のデータにパッと描き換える
    badge_placeholder.empty()
    with main_placeholder.container():
        draw_bus_cards(
            st.session_state.last_results, st.session_state.last_updated_str
        )


render_bus_schedule()