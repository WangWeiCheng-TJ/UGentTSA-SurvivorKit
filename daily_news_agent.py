import os
import time
import feedparser
import gspread
import google.generativeai as genai
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
else:
    genai.configure(api_key=LLM_API_KEY)

# --- 2. 設定區 ---
RSS_URL = "https://stad.gent/nl/nieuws-evenementen/rss"
SHEET_NAME = "Ghent_Survival_DB"
TAB_NAME = "News"
CREDENTIALS_FILE = "googlesheetapi.json"

FETCH_LIMIT = 60
DAYS_LOOKBACK = 30

class NewsAgent:
    def __init__(self):
        self.model = genai.GenerativeModel(MODEL_NAME) if LLM_API_KEY else None
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
        🛡️ 防禦性 AI 呼叫函式
        遇到 429 (Rate Limit) 就睡覺重試，不會直接死掉。
        """
        if not self.model: return None

        for attempt in range(max_retries):
            try:
                response = self.model.generate_content(prompt)
                return response
            except Exception as e:
                error_msg = str(e)
                # 如果是 429 錯誤 (Rate Limit)
                if "429" in error_msg or "quota" in error_msg.lower():
                    wait_time = 60 # 罰站 35 秒 (比 Google 建議的 23 秒多一點以策安全)
                    tqdm.write(f"   ⏳ API 額度滿了，休息 {wait_time} 秒後重試 ({attempt+1}/{max_retries})...")
                    time.sleep(wait_time)
                else:
                    # 如果是其他錯誤 (例如 Server Error)，稍微等一下再試
                    tqdm.write(f"   ⚠️ AI 呼叫錯誤: {e}，重試中...")
                    time.sleep(15)
        
        return None # 試了 3 次都失敗，放棄

    def parse_entry_date_with_ai(self, entry):
        if not self.model or not self.date_prompt_template:
            return datetime.now()
        try:
            entry_dump = f"Title: {entry.title}\nLink: {entry.link}\nPublished: {entry.get('published', 'N/A')}\nUpdated: {entry.get('updated', 'N/A')}\nDesc: {entry.get('description', '')[:200]}"
            prompt = self.date_prompt_template.format(entry_data=entry_dump)
            response = self.model.generate_content(prompt)
            date_text = response.text.strip().replace('"', '').replace("'", "")
            return datetime.strptime(date_text, "%Y-%m-%d")
        except:
            return datetime.now()

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

            pub_date = self.parse_entry_date_with_ai(entry)
            time.sleep(0.3)

            if pub_date >= cutoff_date:
                valid_items.append({
                    "date": pub_date.strftime("%Y-%m-%d"),
                    "title": entry.title, # 還是要傳荷蘭文標題給 AI 分析用
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
            # 預設空值
            level, audience, topic, title_zh, summary, action = "1", "-", "其他", "解析失敗", "AI Error", "-"
            
            if self.model and self.analysis_prompt_template:
                try:
                    prompt = self.analysis_prompt_template.format(title=item['title'])
                    response = self.model.generate_content(prompt)
                    text = response.text.strip()

                    # 從每一行裡找第一行有 5 個以上 '|' 的（即 6 欄格式）
                    # 跳過模型多說的廢話或空行
                    parsed = False
                    for line in text.splitlines():
                        parts = line.split("|")
                        if len(parts) >= 6:
                            level, audience, topic, title_zh, summary, action = [p.strip() for p in parts[:6]]
                            parsed = True
                            break

                    if not parsed:
                        tqdm.write(f"⚠️ 格式解析失敗，原始輸出: {text[:100]}")
                        summary = text[:200]

                except Exception as e:
                    tqdm.write(f"⚠️ AI Content Error: {e}")
            
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
                    tqdm.write(f"❌ 寫入失敗: {e}")
            
            time.sleep(2.0) 

        print(f"🎉 全部完成！Google Sheet 新增 {count} 筆情報。")

if __name__ == "__main__":
    NewsAgent().run()