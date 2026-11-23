import html
import os
import re
import sys
import time
import feedparser
import gspread
import httpx
from google import genai
from google.genai import errors as genai_errors
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from time import mktime
from dotenv import load_dotenv
from tqdm import tqdm 

# ✅ 導入外部 Prompt
def load_prompt_file(filename):
    try:
        file_path = os.path.join(os.path.dirname(__file__), 'prompts', filename)
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return ""

# --- 1. 環境變數 ---
load_dotenv() 

LLM_API_KEY = os.getenv("GOOGLE_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME", "gemma-4-31b-it")

if not LLM_API_KEY:
    print("❌ [Env] 找不到 GOOGLE_API_KEY！")

_genai_client = genai.Client(api_key=LLM_API_KEY) if LLM_API_KEY else None

# --- 2. 設定區 ---
RSS_URL = "https://stad.gent/nl/nieuws-evenementen/rss"
SHEET_NAME = "Ghent_Survival_DB"
TAB_NAME = "News"
CREDENTIALS_FILE = "googlesheetapi.json"

FETCH_LIMIT = 60
DAYS_LOOKBACK = 30


def clean_description(raw):
    """RSS description 轉純文字：去掉 HTML 標籤與實體、壓成一行。沒有內容時回傳空字串。"""
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    return " ".join(text.split())


class AIFatalError(Exception):
    """整個 run 都不該繼續 (額度用完、key 無效、模型不存在等)。"""


class AIItemError(Exception):
    """只有這一筆失敗：不寫入 Sheet，下次執行會自動重試。"""


class NewsAgent:
    def __init__(self):
        self.model = _genai_client
        self.failures = []  # (連結, 原因)
        self.setup_gsheet()
        self.date_prompt_template = load_prompt_file("date_parser.txt")
        self.analysis_prompt_template = load_prompt_file("news_analysis.txt")

    def setup_gsheet(self):
        try:
            scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
            if not os.path.exists(CREDENTIALS_FILE):
                self.sheet = None
                return
            creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
            self.client = gspread.authorize(creds)
            spreadsheet = self.client.open(SHEET_NAME)
            try:
                self.sheet = spreadsheet.worksheet(TAB_NAME)
                print(f"✅ Google Sheet 連線成功: {TAB_NAME}")
            except gspread.WorksheetNotFound:
                self.sheet = spreadsheet.add_worksheet(title=TAB_NAME, rows="100", cols="8")
                # ✅ 更新標題列：Date | Level | Audience | Topic | Title | Summary | Action | Source_URL
                self.sheet.append_row(["Date", "Level", "Audience", "Topic", "Title", "Summary", "Action", "Source_URL"])
        except Exception as e:
            print(f"❌ [Sheet Error] {e}")
            self.sheet = None

    def call_ai_with_retry(self, prompt, max_retries=3):
        """
        🛡️ 防禦性 AI 呼叫函式，成功時回傳非空字串。
        - 429 (Rate Limit)：休息後重試，重試完仍失敗就中止整個 run。
        - 其他 4xx (例如 402 額度用完、403 key 無效、404 模型不存在)：重試沒用，立刻中止。
        - 5xx / 連線錯誤：重試，仍失敗則只跳過這一筆。
        """
        if not self.model:
            raise AIFatalError("找不到 GOOGLE_API_KEY")

        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                response = self.model.models.generate_content(model=MODEL_NAME, contents=prompt)
            except genai_errors.ClientError as e:
                if e.code != 429:
                    raise AIFatalError(str(e)) from e
                last_error, wait_time = e, 60
            except (genai_errors.ServerError, httpx.TransportError) as e:
                last_error, wait_time = e, 15
            else:
                text = (response.text or "").strip()
                if not text:
                    raise AIItemError("AI 回傳空內容")
                return text

            if attempt < max_retries:
                tqdm.write(f"   ⏳ AI 呼叫錯誤: {last_error}，{wait_time} 秒後重試 ({attempt}/{max_retries})...")
                time.sleep(wait_time)

        if isinstance(last_error, genai_errors.ClientError):
            raise AIFatalError(f"重試 {max_retries} 次仍是 429: {last_error}") from last_error
        raise AIItemError(f"重試 {max_retries} 次仍失敗: {last_error}") from last_error

    def record_failure(self, link, reason):
        self.failures.append((link, str(reason)))
        tqdm.write(f"⚠️ 跳過 {link}: {reason}")

    def parse_entry_date_with_ai(self, entry):
        if not self.date_prompt_template:
            raise AIFatalError("找不到 prompts/date_parser.txt")
        entry_dump = f"Title: {entry.title}\nLink: {entry.link}\nPublished: {entry.get('published', 'N/A')}\nUpdated: {entry.get('updated', 'N/A')}\nDesc: {entry.get('description', '')[:200]}"
        prompt = self.date_prompt_template.format(entry_data=entry_dump)
        text = self.call_ai_with_retry(prompt)

        # 模型可能多說廢話；只有剛好出現一個日期時才採用
        dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", text))
        if len(dates) == 1:
            try:
                return datetime.strptime(dates.pop(), "%Y-%m-%d")
            except ValueError as e:
                raise AIItemError(f"日期解析失敗: {text[:100]}") from e
        # prompt 約定：無法判斷日期時輸出 TODAY
        if not dates and text.strip("\"'. ").upper() == "TODAY":
            return datetime.now()
        raise AIItemError(f"日期解析失敗: {text[:100]}")

    def analyze_news(self, title, description):
        if not self.analysis_prompt_template:
            raise AIFatalError("找不到 prompts/news_analysis.txt")
        text = self.call_ai_with_retry(self.analysis_prompt_template.format(
            title=title, description=description or "(無)"))

        # 從每一行裡找第一行有 5 個以上 '|' 的（即 6 欄格式）
        # 跳過模型多說的廢話或空行
        for line in text.splitlines():
            parts = line.split("|")
            if len(parts) >= 6:
                return [p.strip() for p in parts[:6]]
        raise AIItemError(f"格式解析失敗，原始輸出: {text[:100]}")

    def fetch_and_filter(self):
        print(f"📡 連線 RSS 伺服器...")
        feed = feedparser.parse(RSS_URL)
        if not feed.entries: return []
        
        existing_links = []
        if self.sheet:
            try:
                # 連結現在變成第 8 欄 (H欄)
                existing_links = self.sheet.col_values(8)
            except: pass

        cutoff_date = datetime.now() - timedelta(days=DAYS_LOOKBACK)
        valid_items = []
        
        raw_entries = feed.entries[:FETCH_LIMIT]
        for entry in tqdm(raw_entries, desc="🔍 AI 全文掃描日期", unit="筆"):
            link = entry.link
            if link in existing_links: continue

            try:
                pub_date = self.parse_entry_date_with_ai(entry)
            except AIItemError as e:
                self.record_failure(link, e)
                continue
            time.sleep(0.3)

            if pub_date >= cutoff_date:
                valid_items.append({
                    "date": pub_date.strftime("%Y-%m-%d"),
                    "title": entry.title, # 還是要傳荷蘭文標題給 AI 分析用
                    "description": clean_description(entry.get("description", "")),
                    "link": link
                })

        print(f"✅ 過濾完成！發現 {len(valid_items)} 筆 {DAYS_LOOKBACK} 天內的新資料。")
        return valid_items

    def run(self):
        news_list = self.fetch_and_filter()
        if not news_list:
            print("🎉 沒有新資料需處理。")
            return

        print("🚀 開始 AI 戰略分析 (根特土地公模式)...")
        count = 0
        
        for item in tqdm(news_list, desc="🤖 生成情報", unit="筆"):
            try:
                level, audience, topic, title_zh, summary, action = self.analyze_news(
                    item['title'], item['description'])
            except AIItemError as e:
                # 不寫入 Sheet：一旦連結進了 H 欄，之後就不會再重跑
                self.record_failure(item['link'], e)
                continue

            icon = "🟢"
            if "3" in level: icon = "🔴"
            elif "2" in level: icon = "🟡"

            # 顯示這則新聞的中文標題 (讓你知道它生成了什麼)
            tqdm.write(f"   {icon} [{item['date']}] [{level}] [{audience}] [{topic}] {title_zh} ({action}): {summary}")

            if self.sheet:
                try:
                    # ✅ 寫入順序對應你的要求
                    self.sheet.append_row([
                        item['date'], 
                        level, 
                        audience, 
                        topic, 
                        title_zh,   # 10字中文標題
                        summary,    # 繁體中文摘要
                        action, 
                        item['link']
                    ])
                    count += 1
                except Exception as e:
                    self.record_failure(item['link'], f"寫入失敗: {e}")
            
            time.sleep(2.0) 

        print(f"🏁 處理完成：Google Sheet 新增 {count} / 應處理 {len(news_list)} 筆情報。")

def main():
    agent = NewsAgent()
    try:
        agent.run()
    except AIFatalError as e:
        # ::error:: 會顯示在 GitHub Actions 的 run 摘要
        print(f"::error::AI 無法使用，已中止: {e}")
        return 1
    if agent.failures:
        print(f"::error::有 {len(agent.failures)} 筆失敗，未寫入 Sheet，下次執行會重試")
        for link, reason in agent.failures:
            print(f"   - {link}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
