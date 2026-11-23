import type { NewsItem } from "@/components/MainView";

// 幹部手動公告的主題；AI 的主題清單裡沒有這個值，不會撞到
export const ANNOUNCEMENT_TOPIC = "置頂公告";

// 跟 daily_news_agent.py 的 DAYS_LOOKBACK 一致
export const PINNED_DAYS = 30;

const isAnnouncement = (item: NewsItem) => item.Topic === ANNOUNCEMENT_TOPIC;

// 日期讀不懂時當成 0：排到最後，但不會因為過期而消失
const toTime = (item: NewsItem) => new Date(item.Date).getTime() || 0;

// 新的在前
export function sortByDateDesc(news: NewsItem[]): NewsItem[] {
  return [...news].sort((a, b) => toTime(b) - toTime(a));
}

// 置頂區：公告永遠在最前面且不過期，其他只留近 PINNED_DAYS 天
export function getPinnedNews(news: NewsItem[], now: Date = new Date()): NewsItem[] {
  const cutoff = now.getTime() - PINNED_DAYS * 24 * 60 * 60 * 1000;
  const sorted = sortByDateDesc(news.filter((item) => String(item.Level) === "2"));
  return [
    ...sorted.filter(isAnnouncement),
    ...sorted.filter((item) => !isAnnouncement(item) && (toTime(item) === 0 || toTime(item) >= cutoff)),
  ];
}
