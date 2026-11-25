import { Link } from "react-router-dom"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ModeToggle } from "@/components/mode-toggle"
import { Logo } from "@/components/brand/logo"
import { useSidebarStore } from "@/stores/sidebar"
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from "@/components/ui/tooltip"
import { Menu } from "lucide-react"

export function Topbar() {
  const toggle = useSidebarStore((s) => s.toggle)
  const env = (import.meta.env.VITE_ENV || import.meta.env.MODE || '').toLowerCase()
  const envLabel = env === 'production' || env === 'prod'
    ? 'prod'
    : env === 'staging'
      ? 'staging'
      : 'dev'
  return (
    <TooltipProvider>
      <header className="flex h-12 items-center justify-between shadow-md px-md border-0">
        <div className="flex items-center gap-md">
          <Link
            to="/dashboard"
            aria-label="TradeLense home"
            className="flex items-center rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          >
            {/* Icon variant, not the wide mark: the wide mark carries its focus frame
                in the middle of a 51px box, which reads as 16px right of the sidebar
                icons below it. The icon's own artwork is inset ~14% inside its box, so
                the -ml-1 nudge lands the frame in the same column as those icons.
                Wordmark drops on narrow screens; the icon stays above its 16px minimum. */}
            <Logo
              size={28}
              variant="icon"
              wordmarkSize={20}
              className="-ml-1"
              wordmarkClassName="hidden sm:inline"
            />
          </Link>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="icon" className="md:hidden" onClick={toggle} aria-label="Toggle sidebar">
                <Menu className="h-5 w-5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">Toggle sidebar</TooltipContent>
          </Tooltip>
        </div>
        <div className="flex items-center gap-sm">
          <Badge variant="secondary" className="uppercase">{envLabel}</Badge>
          <ModeToggle />
        </div>
      </header>
    </TooltipProvider>
  )
}
