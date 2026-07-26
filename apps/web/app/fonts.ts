import { Be_Vietnam_Pro, JetBrains_Mono } from "next/font/google";

// Thân bài và tiêu đề dùng chung một họ sans. Be Vietnam Pro được thiết kế riêng
// cho tiếng Việt nên dấu thanh cân, không chồng lên dấu mũ như phần lớn font Latin.
// Tiêu đề phân biệt bằng chữ đậm + giãn chữ hẹp qua .font-display.
export const sans = Be_Vietnam_Pro({
  subsets: ["latin", "latin-ext", "vietnamese"],
  weight: ["300", "400", "500", "600", "700"],
  variable: "--font-sans-family",
  display: "swap",
});

// Mã nguồn / dữ liệu
export const jbmono = JetBrains_Mono({
  subsets: ["latin", "latin-ext", "vietnamese"],
  variable: "--font-jbmono",
  display: "swap",
});

export const fontVars = `${sans.variable} ${jbmono.variable}`;
