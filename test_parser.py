"""測試 AI 解析格式是否正確"""
import os
from google import genai
from dotenv import load_dotenv

load_dotenv()
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
MODEL_NAME = os.getenv("MODEL_NAME", "models/gemma-4-31b-it")

def load_prompt(filename):
    with open(f"prompts/{filename}", encoding="utf-8") as f:
        return f.read()

template = load_prompt("news_analysis.txt")

test_titles = [
    "Stad Gent kondigt nieuwe parkeerregels aan in het stadscentrum",
    "Staking bij De Lijn op vrijdag treft tram- en buslijnen in Gent",
    "Gentse universiteiten verwelkomen recordaantal internationale studenten",
]

for title in test_titles:
    print(f"\n📰 原標題: {title}")
    prompt = template.format(title=title)
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    text = response.text.strip()
    print(f"🤖 原始輸出: {repr(text)}")

    parsed = False
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) >= 6:
            level, audience, topic, title_zh, summary, action = [p.strip() for p in parts[:6]]
            print(f"✅ 解析成功: [{level}] [{audience}] [{topic}] {title_zh} | {summary} | {action}")
            parsed = True
            break

    if not parsed:
        print(f"❌ 解析失敗，找不到 6 欄格式")
