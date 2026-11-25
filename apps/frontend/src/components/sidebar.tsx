import { NavLink } from "react-router-dom"
import { cn } from "@/lib/utils"
import { BrandMark, Wordmark } from "@/components/brand/logo"
import { useSidebarStore } from "@/stores/sidebar"
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from "@/components/ui/tooltip"
import {
  Home,
  Bot,
  FileText,
  UserCog,
  Brain,
  Cpu,
  BarChart3,
  Sparkles
} from "lucide-react"

export function Sidebar() {
  const open = useSidebarStore((s) => s.open)
  const toggle = useSidebarStore((s) => s.toggle)
  const close = useSidebarStore((s) => s.close)

  const links = [
    { to: "/", label: "Home", icon: Home },
    { to: "/agents", label: "Agents", icon: Bot },
    { to: "/configs/model-selection", label: "Model Selection", icon: Cpu },
    { to: "/unified-analysis", label: "Trading Analysis", icon: Brain },
    { to: "/backtests", label: "Backtests", icon: BarChart3 },
    { to: "/logs", label: "Logs", icon: FileText },
    { to: "/admin", label: "Admin", icon: UserCog },
    { to: "/about", label: "About", icon: Sparkles }
  ]
  return (
    <TooltipProvider>
      <aside
        className={cn(
          "group flex flex-col gap-sm shadow-lg bg-muted p-sm transition-all duration-300 overflow-y-auto scrollbar-hide border-0",
          open ? "w-64" : "w-16"
        )}
        onMouseEnter={() => !open && toggle()}
        onMouseLeave={() => open && close()}
        onClick={close}
      >
        {links.map((link) => (
          <Tooltip key={link.to}>
            <TooltipTrigger asChild>
              <NavLink
                to={link.to}
                end
                className={({ isActive }) =>
                  cn(
                    "flex items-center gap-3 rounded-md px-sm py-sm text-sm font-medium transition-colors",
                    isActive
                      ? "bg-accent text-accent-foreground"
                      : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                  )
                }
              >
                <link.icon className="h-5 w-5" />
                <span
                  className={cn(
                    "whitespace-nowrap transition-opacity duration-300",
                    open ? "opacity-100" : "opacity-0"
                  )}
                >
                  {link.label}
                </span>
              </NavLink>
            </TooltipTrigger>
            <TooltipContent side="right">{link.label}</TooltipContent>
          </Tooltip>
        ))}

        {/* Brand signature at the foot of the rail: icon while collapsed, the
            wordmark fades in with the rail. */}
        <div className="mt-auto flex items-center gap-3 px-sm pb-sm pt-md">
          <BrandMark size={20} />
          <Wordmark
            size={15}
            className={cn(
              "whitespace-nowrap transition-opacity duration-300",
              open ? "opacity-100" : "opacity-0"
            )}
          />
        </div>
      </aside>
    </TooltipProvider>
  )
}
