import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { AppRouter } from "@/app/routes"
import { ThemeProvider } from "@/app/theme-provider"
import { Toaster } from "sonner"

const queryClient = new QueryClient()

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <AppRouter />
        <Toaster richColors />
      </ThemeProvider>
    </QueryClientProvider>
  )
}
