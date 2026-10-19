import asyncio
import datetime
from pathlib import Path
import re
import subprocess
import threading
import time
import pytz
import streamlit as st
from playwright.async_api import Playwright, async_playwright, expect

st.set_page_config(page_title="バス発車案内", page_icon="🚌", layout="wide")

JST = pytz.timezone("Asia/Tokyo")
BASE_DIR = Path(__file__).parent

# PC版のダミーデータ
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


# --- 外部ファイル（style.css と template.html）を読み込む関数 ---
def load_css(file_name="style.css"):
    css_path = BASE_DIR / file_name
    if css_path.exists():
        css_content = css_path.read_text(encoding="utf-8")
        st.markdown(f"<style>{css_content}</style>", unsafe_allow_html=True)


def load_html_templates(file_name="template.html"):
    html_path = BASE_DIR / file_name
    templates = {}
    if not html_path.exists():
        return templates
    raw = html_path.read_text(encoding="utf-8")
    # <!-- === NAME === --> ごとに分割して辞書化
    parts = re.split(r"<!--\s*===\s*(\w+)\s*===\s*-->", raw)
    for i in range(1, len(parts), 2):
        section_name = parts[i].strip()
        section_body = parts[i + 1].strip()
        templates[section_name] = section_body
    return templates


# CSSの適用
load_css("style.css")


# --- 1. 初回起動時にPlaywrightのChromiumをインストール ---
@st.cache_resource
def install_playwright_browser():
    subprocess.run(["playwright", "install", "chromium"], check=True)


install_playwright_browser()


# --- 2. スクレイピングクラス ---
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


# --- 4. PC版の _parse_results ロジック ---
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


# --- 5. 設定バー ---
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
    templates = load_html_templates("template.html")
    if not templates:
        st.error("template.html が見つかりません。リポジトリに作成してください。")
        return

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
                '<div class="corner-status text-red">⚠️ 取得エラー</div>',
                unsafe_allow_html=True,
            )
            st.error(f"スクレイピングエラー: {e}")
            return

    top_items, bottom_items = parse_results_like_pc(raw_results)

    # 1. ヘッダー部分のHTML組み立て
    html_parts = [
        templates["HEADER"].format(
            start_stop=st.session_state.start_stop,
            end_stop=st.session_state.end_stop,
        )
    ]

    # 2. 上段（第1便）のHTML組み立て
    if top_items:
        item = top_items[0]
        info_code = item.get("info", 0)

        if info_code == 403:
            html_parts.append(
                templates["MESSAGE_ROW"].format(
                    msg_color_class="text-red",
                    message="サイトへのアクセスがタイムアウトしました",
                )
            )
        elif info_code == -2:
            html_parts.append(
                templates["MESSAGE_ROW"].format(
                    msg_color_class="text-white",
                    message="本日の運行は終了しました",
                )
            )
        elif info_code != -9 and item.get("line") != "--":
            minute_str = item.get("delay", "")
            rem_color_class = "text-white"
            pro_color_class = "text-white"
            if minute_str == "正常運行":
                rem_color_class = "text-green"
            elif minute_str == "少し遅れ":
                rem_color_class, pro_color_class = "text-orange", "text-orange"
            elif re.match(r"約(\d+)分遅れ", minute_str):
                rem_color_class, pro_color_class = "text-red", "text-orange"

            line_str = item.get("line", "")
            line_bg_class = "bg-orange" if line_str in ["直行", "急行"] else "bg-blue"

            remarks = []
            if line_str in ["直行", "急行"]:
                remarks.append(f'<span class="text-orange">{line_str}</span>')
            if info_code == -3:
                remarks.append('<span class="text-red">最終</span>')
            if item.get("starting_stop"):
                remarks.append('<span class="text-white">始発</span>')
            remark_html = " / ".join(remarks)

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
                arrival_html = '<div class="arrival-countdown blink-soon">まもなく到着</div>'
            elif 2 < arrival_min < 31:
                arrival_html = f'<div class="arrival-countdown">到着まで約 {arrival_min} 分</div>'
            elif arrival_min <= 2:
                arrival_html = '<div class="arrival-countdown blink-soon">まもなく到着</div>'
            else:
                arrival_html = '<div class="arrival-countdown">&nbsp;</div>'

            stops_str = "、".join([s for s in item.get("stops", []) if s])
            stops_str = " ➔ ".join([s for s in item.get("stops", []) if s])

            html_parts.append(
                templates["TOP_ITEM"].format(
                    dep_time=item.get("dep_time", ""),
                    line_bg_class=line_bg_class,
                    line_str=line_str,
                    destination=item.get("destination", ""),
                    rem_color_class=rem_color_class,
                    minute_str=minute_str,
                    remark_html=remark_html,
                    status=item.get("status", ""),
                    arrival_html=arrival_html,
                    stops_str=stops_str if stops_str else "（直行・途中停留所なし）",
                    pro_color_class=pro_color_class,
                    pro_dep_time=item.get("pro_dep_time", ""),
                    pro_arr_time=item.get("pro_arr_time", ""),
                    start_stop=st.session_state.start_stop,
                    end_stop=st.session_state.end_stop,
                )
            )
        else:
            html_parts.append(
                templates["MESSAGE_ROW"].format(
                    msg_color_class="text-gray",
                    message="現在接近中のバスはありません",
                )
            )

    # 3. 下段（第2便・第3便）のHTML組み立て
    if bottom_items:
        html_parts.append('<div class="section-divider"></div>')
        for b_item in bottom_items:
            b_min = b_item.get("delay", "")
            b_color_class = "text-white"
            if b_min == "正常運行":
                b_color_class = "text-green"
            elif b_min == "少し遅れ":
                b_color_class = "text-orange"
            elif re.match(r"約(\d+)分遅れ", b_min):
                b_color_class = "text-red"

            b_line = b_item.get("line", "")
            b_bg_class = "bg-orange" if b_line in ["直行", "急行"] else "bg-blue"

            b_remarks = []
            if b_line in ["直行", "急行"]:
                b_remarks.append(f'<span class="text-orange">{b_line}</span>')
            if b_item.get("info") == -3:
                b_remarks.append('<span class="text-red">最終</span>')
            if b_item.get("starting_stop"):
                b_remarks.append('<span class="text-white">始発</span>')
            b_rem_html = " / ".join(b_remarks)

            html_parts.append(
                templates["BOTTOM_ITEM"].format(
                    dep_time=b_item.get("dep_time", ""),
                    line_bg_class=b_bg_class,
                    line_str=b_line,
                    destination=b_item.get("destination", ""),
                    rem_color_class=b_color_class,
                    minute_str=b_min,
                    remark_html=b_rem_html,
                )
            )

    # インデントを除去してHTMLとして描画
    clean_html = "\n".join(
        line.strip() for line in "".join(html_parts).splitlines() if line.strip()
    )
    st.markdown(clean_html, unsafe_allow_html=True)

    # フッター
    f_col1, f_col2, f_col3 = st.columns([6, 2, 2])
    with f_col1:
        mode_str = (
            "🧪 ダミーデータ表示中"
            if st.session_state.use_dummy
            else f"最終取得: {now_jst.strftime('%H:%M:%S')} (所要 {elapsed:.1f}秒)"
        )
        st.caption(f"⏱ {mode_str} ／ 20秒ごとに自動更新")
    with f_col2:
        if st.button("🔄 手動更新", use_container_width=True, key="signage_refresh"):
            st.rerun()
    with f_col3:
        with st.popover("📄 JSON確認", use_container_width=True):
            st.json(raw_results)


render_signage()