import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * Brain là một người dùng cho mỗi project, chạy trên máy của chính người đó — không có trang
 * đăng nhập để chuyển hướng tới. Phiên được mở tự động ở `AppShell`, nên middleware chỉ còn
 * đúng một việc: đưa `/` về màn hình làm việc.
 */
export function middleware(req: NextRequest) {
  if (req.nextUrl.pathname === "/") {
    const url = req.nextUrl.clone();
    url.pathname = "/search";
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.).*)"],
};
