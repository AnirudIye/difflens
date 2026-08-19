import { NextResponse, type NextRequest } from "next/server";

// Pure UX guard: presence of the cookie is not authentication, the API is
// the real gate. This only spares signed-out visitors a useless page load.
export function middleware(request: NextRequest) {
  if (!request.cookies.has("session")) {
    return NextResponse.redirect(new URL("/login", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/dashboard", "/repositories", "/repositories/:path*", "/settings"],
};
