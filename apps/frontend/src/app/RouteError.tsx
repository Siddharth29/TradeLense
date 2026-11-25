import { isRouteErrorResponse, useRouteError, Link } from "react-router-dom"
import { Logo } from "@/components/brand/logo"

export default function RouteError() {
  const error = useRouteError()

  let title = "Something went wrong"
  let message = "An unexpected error occurred."
  let status: number | undefined

  if (isRouteErrorResponse(error)) {
    status = error.status
    title = `${error.status} ${error.statusText}`
    message = (error.data as any)?.message || (error.data as any)?.detail || message
  } else if (error instanceof Error) {
    message = error.message
  }

  return (
    <div className="min-h-screen flex flex-col items-center justify-center gap-lg p-lg">
      {/* This route renders outside the app layout, so it carries its own logo. */}
      <Link to="/" aria-label="TradeLense home">
        <Logo size={32} />
      </Link>
      <div className="max-w-lg w-full border rounded-lg p-lg shadow-sm">
        <h1 className="text-xl font-semibold mb-2">{title}</h1>
        {status && (
          <p className="text-sm text-gray-500 mb-2">Status: {status}</p>
        )}
        <p className="mb-4 break-words">{message}</p>
        <div className="flex gap-3">
          <Link to="/" className="underline text-blue-600">Go home</Link>
          <button onClick={() => location.reload()} className="underline text-blue-600">Reload</button>
        </div>
      </div>
    </div>
  )
}

