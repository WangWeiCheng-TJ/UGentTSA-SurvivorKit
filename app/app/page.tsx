import React from "react";
import MainView, { NewsItem } from "@/components/MainView"; // 👈 關鍵：引入 MainView

// 設定 API URL
const API_URL =
  "https://script.google.com/macros/s/AKfycbxwQDho31iu9GvwTiG3NeXbOZn1-9U60HY_2MzUkjp0hmYsZ2FnU8yw7UDQncV06Qf5/exec";
// === Server Side Fetching (這是在伺服器跑的) ===
async function getNews() {
  try {
    // ✅ 恢復了 ISR 60秒更新！Vercel 伺服器會幫你擋流量，每分鐘才去煩 Google 一次
    const fetchOptions = process.env.NODE_ENV === 'development'
      ? { cache: 'no-store' as const }
      : { next: { revalidate: 60 } };
    const res = await fetch(API_URL, fetchOptions);
    if (!res.ok) {
      throw new Error("Failed to fetch data");
    }
    return res.json();
  } catch (error) {
    console.error("Error fetching news:", error);
    return [];
  }
}

// 1. 定義 Props 型別，告訴 Next.js 這一頁會收到搜尋參數
// 注意：在 Next.js 15，searchParams 是一個 Promise，必須等待
type Props = {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
};

// 2. 把 searchParams 放進參數裡
export default async function Home({ searchParams }: Props) {
  // A. 在伺服器端抓好資料
  const newsData = await getNews();

  // B. 解析網址參數 (等待 Promise 解析)
  const resolvedSearchParams = await searchParams;
  
  // C. 抓出 tab 的值，如果沒傳或格式不對，就預設用 "home"
  const tabParam = resolvedSearchParams.tab;
  const initialTab = typeof tabParam === 'string' ? tabParam : "home";

  // D. 把 initialTab 傳給 MainView
  return (
    <main className="min-h-screen bg-gray-50 pb-24 text-gray-900">
      <MainView initialNewsData={newsData} initialTab={initialTab} />
    </main>
  );
}