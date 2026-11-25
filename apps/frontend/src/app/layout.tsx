import { Outlet, useLocation } from "react-router-dom"
import { Sidebar } from "@/components/sidebar"
import { Topbar } from "@/components/topbar"
import { Breadcrumbs } from "@/components/breadcrumbs"
import { TooltipProvider } from "@/components/ui/tooltip"

export default function Layout() {
  const { pathname } = useLocation()
  const compactPage = ["/dashboard", "/agents", "/unified-analysis"].includes(pathname)

  return (
    <TooltipProvider>
      <div className="flex h-screen flex-col overflow-hidden">
        <Topbar />
        <div className="flex flex-1 overflow-hidden">
          <Sidebar />
          <main className="flex-1 overflow-auto">
            <div className={`${compactPage ? "p-4" : "p-lg"} h-full flex flex-col`}>
              {!compactPage && <Breadcrumbs />}
              <div className="flex-1 overflow-auto">
                <Outlet />
              </div>
            </div>
          </main>
        </div>
      </div>
    </TooltipProvider>
  )
}
