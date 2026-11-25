import { Link, useLocation } from "react-router-dom"

function toTitle(segment: string) {
  return segment
    .split("-")
    .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
    .join(" ")
}

export function Breadcrumbs() {
  const location = useLocation()
  const segments = location.pathname.split("/").filter(Boolean)

  return (
    <nav aria-label="breadcrumb" className="mb-4 text-sm">
      <ol className="flex items-center gap-xs">
        <li>
          <Link to="/" className="hover:underline">
            Home
          </Link>
        </li>
        {segments.map((segment, idx) => {
          const path = "/" + segments.slice(0, idx + 1).join("/")
          const isLast = idx === segments.length - 1
          return (
            <li key={path} className="flex items-center gap-xs">
              <span>/</span>
              {isLast ? (
                <span className="text-muted-foreground">{toTitle(segment)}</span>
              ) : (
                <Link to={path} className="hover:underline">
                  {toTitle(segment)}
                </Link>
              )}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}
